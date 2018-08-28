from ellie import data_products
from ellie import find_sources
from ellie import visualize
from astropy.io import fits
import numpy as np
import matplotlib.pyplot as plt



#tic  = 198593129
#pos  = [266.491526, 49.518268]
gaia = 1414861664385248640
#a = data_products(gaia=gaia)
#a.individual_tpf()
#a.custom_aperture(shape='circle', r=2.3, pointing=False, jitter=False)

#b = visualize(tic=tic)
b = visualize(gaia=gaia)
b.mark_gaia()
#b.tpf_movie(plot_lc=True, aperture=True)
#lc = b.click_aperture()
#print(lc)
#plt.plot(np.arange(0,len(lc),1), lc, 'r')
#plt.show()

#a = data_products()
#for i in np.arange(1,5,1):
#    for j in np.arange(1,5,1):
#        print('camera = {}; chip = {}'.format(i,j))
#        a.make_postcard(camera=i, chip=j, sector=1)
