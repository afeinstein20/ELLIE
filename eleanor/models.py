import math
import os
from astropy.io import fits as pyfits
from lightkurve.utils import channel_to_module_output
import numpy as np
import warnings
import torch
import scipy.special
import scipy.optimize as sopt
from functools import reduce
from abc import ABC
from zernike import Zern, RZern
from matplotlib import pyplot as plt

# Vaneska models of Ze Vinicius

class Model:
	"""
	Base PSF-fitting model.

	Attributes
	----------
	shape : tuple
		shape of the TPF. (row_shape, col_shape)
	col_ref, row_ref : int, int
		column and row coordinates of the bottom
		left corner of the TPF
	"""
	def __init__(self, shape, col_ref, row_ref, xc, yc, bkg0, loss, source, **kwargs):
		self.shape = shape
		self.col_ref = col_ref
		self.row_ref = row_ref
		self.xc = xc
		self.yc = yc
		self.bkg0 = bkg0
		self._init_grid()
		self.bounds = np.vstack((
				np.tile([0, np.infty], (len(self.xc), 1)), # fluxes on each star
				np.array([
					[-2.0, 2.0], # xshift of the star to fit
					[-2.0, 2.0], # yshift of the star to fit
					[0, np.infty] # background average
				])
		))
		self.loss = loss
		from .prf import make_prf_from_source
		self.prf = make_prf_from_source(source)

	def __call__(self, *params):
		return self.evaluate(*params)

	def evaluate(self, *args):
		pass

	def _init_grid(self):
		r, c = self.row_ref, self.col_ref
		s1, s2 = self.shape
		self.y, self.x = np.mgrid[r:r+s1-1:1j*s1, c:c+s2-1:1j*s2]
		self.x = torch.tensor(self.x)
		self.y = torch.tensor(self.y)

	def mean(self, flux, xshift, yshift, bkg, optpars, norm=True):
		return sum([self.evaluate(flux[j], xshift, yshift, optpars, j, norm) for j in range(len(self.xc))]) + bkg

	def get_default_par(self, d0):
		return np.concatenate((
			np.max(d0) * np.ones(len(self.xc),),
			np.array([0, 0, self.bkg0]), 
			self.get_default_optpars()
		))

	def evaluate(self, flux, xs, ys, params, j, norm=True):
		dx = self.x - (self.xc[j] + xs)
		dy = self.y - (self.yc[j] + ys)
		psf = self.psf(dx, dy, params, j)
		if norm:
			psf_sum = torch.sum(psf)
		else:
			psf_sum = torch.tensor(1.)
		return flux * psf / psf_sum

class PRFWrap(Model):
	def __init__(self, **kwargs):
		self.submodel = eval(kwargs.get('model'))(**kwargs)
	
	def get_default_optpars(self):
		return self.submodel.get_default_optpars()
	
	def psf(self, dx, dy, params, j):
		return torch.tensor(scipy.signal.convolve2d(self.submodel.psf(dx, dy, params, j), self.prf))

class Gaussian(Model):
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		self.bounds = np.vstack((
				self.bounds,
				np.array([
					[0, np.infty],
					[-0.5, 0.5],
					[0, np.infty],
				])
			))

	def get_default_optpars(self):
		return np.array([1, 0, 1], dtype=np.float64)

	def psf(self, dx, dy, params, j):
		"""
		The Gaussian model
		Parameters
		----------
		flux : np.ndarray, (len(self.xc),)
		xo, yo : scalar
			Center coordinates of the Gaussian.
		a, b, c : scalar
			Parameters that control the rotation angle
			and the stretch along the major axis of the Gaussian,
			such that the matrix M = [a b ; b c] is positive-definite.
		References
		----------
		https://en.wikipedia.org/wiki/Gaussian_function#Two-dimensional_Gaussian_function
		"""
		a, b, c = params
		return torch.exp(-(a * dx ** 2 + 2 * b * dx * dy + c * dy ** 2))

class Moffat(Model):
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		self.bounds = np.vstack((
				self.bounds,
				np.array([
					[0, np.infty],
					[-0.5, 0.5],
					[0, np.infty],
					[0.0, np.infty]
				])
			))

	def get_default_optpars(self):
		return np.array([1, 0, 1, 1], dtype=np.float64) # a, b, c, beta

	def psf(self, params, dx, dy, j):
		a, b, c, beta = params
		return torch.true_divide(torch.tensor(1.), (1. + a * dx ** 2 + 2 * b * dx * dy + c * dy ** 2) ** (beta ** 2))

# from https://discuss.pytorch.org/t/modified-bessel-function-of-order-0/18609/2
# we'll always use a Bessel function of the first kind for the Airy disk, i.e. nu = 1.
class ModifiedBesselFn(torch.autograd.Function):
    @staticmethod
    def forward(ctx, inp, nu):
        ctx._nu = nu
        ctx.save_for_backward(inp)
        return torch.from_numpy(scipy.special.iv(nu, inp.detach().numpy()))

    @staticmethod
    def backward(ctx, grad_out):
        inp, = ctx.saved_tensors
        nu = ctx._nu
        # formula is from Wikipedia
        return 0.5* grad_out *(ModifiedBesselFn.apply(inp, nu - 1.0)+ModifiedBesselFn.apply(inp, nu + 1.0)), None

modified_bessel = ModifiedBesselFn.apply

class Airy(Model):
	'''
	Airy disk model. Currently untested.
	'''
	def __init__(self, shape, col_ref, row_ref, **kwargs):
		warnings.warn("This model is still being tested and may yield incorrect results.")
		super().__init__(shape, col_ref, row_ref, **kwargs)

	def psf(self, dx, dy, params, j):
		Rn = params # Rn = R / Rz implicitly; "R normalized"
		r = torch.sqrt(dx ** 2 + dy ** 2)
		bessel_arg = np.pi * r / Rn
		return torch.pow(2 * modified_bessel(torch.tensor(1), bessel_arg) / bessel_arg, 2)

class TorchZern(Zern):
	def __init__(self, n, normalise=Zern.NORM_NOLL):
		super().__init__(n, normalise)
		self.numpy_dtype = np.float32

	# almost a clone of zernike.RZern, but with Torch operations
	def Rnm(self, k, rho):
		# Horner's method, but differentiable
		return reduce(lambda c, r: c * rho + r, self.rhotab[k,:])

	def ck(self, n, m):
		if self.normalise == self.NORM_NOLL:
			if m == 0:
				return np.sqrt(n + 1.0)
			else:
				return np.sqrt(2.0 * (n + 1.0))
		else:
			return 1.0

	def angular(self, j, theta):
		m = self.mtab[j]
		if m >= 0:
			return torch.cos(m * theta)
		else:
			return torch.sin(-m * theta)

		
class Zernike(Model):
	'''
	Fit the Zernike polynomials to the PRF, possibly after a fit from one of the other models.
	'''
	def __init__(self, shape, col_ref, row_ref, xc, yc, bkg0, loss, source, zern_n=4):
		cutoff = 0
		super().__init__(shape, col_ref, row_ref, xc, yc, bkg0, loss, source)
		z = TorchZern(zern_n)
		rz = RZern(zern_n)
		self.z = z
		def gaussian_with_zernike(coords, params):
			A, xo, yo, a, offset = params[:5]
			dx, dy = coords[0] - xo, coords[1] - yo
			g = offset + A * np.exp(-a * (dx ** 2 + dy ** 2)) * sum([p * rz.angular(i, np.arctan2(dy, dx)) for i, p in enumerate(params[5:])])
			return g
		
		p0 = np.concatenate(([1, 0, 0, 1e-3, 0], np.zeros(self.z.nk,)))
		psf_x, psf_y = np.meshgrid(np.linspace(-1, 1, 117), np.linspace(-1, 1, 117))
		res = sopt.minimize(lambda p: np.sum((self.prf - gaussian_with_zernike((psf_x, psf_y), p)) ** 2), p0, method='TNC', tol=1e-1)
		self.zpars = res.x[5:]
		self.zpars = np.array([0.2567228, 0.45743718, -0.28847825, 0.2567228, -0.0757324,  0.11844995, -0.28847825,  0.45743718, -0.04414153, -0.04167375,  0.2567228 ,  0.11844995, -0.0757324, 0.02526562,  0.07075336])
		self.mode_mask = (np.abs(self.zpars) > cutoff).astype(int)
		
		self.cache = {}
		self.coords = {}
		for j in range(len(self.xc)):
			self.cache[j] = {}
			rho, theta = self.get_polar_coords(xc[j], yc[j])
			self.coords[j] = (rho, theta)
			for k in range(z.nk):
				if self.mode_mask[k]:
					zern = torch.tensor(z.angular(k, theta))
					self.cache[j][k] = zern #/ torch.sum(zern)
				elif not(self.mode_mask[k]):
					self.cache[j][k] = torch.zeros(shape)

	def get_polar_coords(self, xo, yo):
		dx = self.x - xo
		dy = self.y - yo
		rho = torch.sqrt(dx ** 2 + dy ** 2)
		theta = torch.atan2(dy, dx)
		return rho, theta

	def get_default_optpars(self):
		return np.concatenate(([0.53327668, 0.53815343], self.zpars))

	def psf(self, dx, dy, params, j):
		(a, c), zpars = params[:2], params[2:]
		psf_c = torch.zeros(self.shape)
		for i in range(len(zpars)):
			b, p = self.mode_mask[i], zpars[i]
			if b:
				psf_c += p * self.cache[j][i]
		return torch.exp(psf_c) * torch.exp(-a * dx ** 2 - c * dy ** 2)
		# the full ellipse will overfit; the rest should show up as Zernikes
