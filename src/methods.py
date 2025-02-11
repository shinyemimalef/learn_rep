
import os  #, traceback, ipdb
#os.environ["CUDA_VISIBLE_DEVICES"]="0"
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributions as distrib

import numpy as np
import matplotlib.pyplot as plt

from omnibelt import get_printer

import omnifig as fig

prt = get_printer(__name__)

try:
	import umap, shap
	import umap.plot
	import gpumap
except ImportError:
	prt.info('umap not found')
from sklearn.decomposition import PCA

import omnilearn as fd
from omnilearn import models
from omnilearn import util
from omnilearn.models.unsup import Autoencoder as SimpleAutoencoder, Generative_AE, Variational_Autoencoder, Wasserstein_Autoencoder
from omnilearn import viz as viz_util
from omnilearn.data.collectors import MissingFIDStatsError
# from foundation import train as trn

# if 'FOUNDATION_RUN_MODE' in os.environ and os.environ['FOUNDATION_RUN_MODE'] == 'jupyter':
# 	from tqdm import tqdm_notebook as tqdm
# else:
from tqdm import tqdm

# import encoders
# import pointnets
# from . import transfer, visualizations as viz_util

MY_PATH = os.path.dirname(os.path.abspath(__file__))



# region Algorithms

@fig.Component('ae')
class Autoencoder(SimpleAutoencoder):
	
	def __init__(self, A, skip_expensive=None, **kwargs):
		
		if skip_expensive is None:
			skip_expensive = A.pull('skip-expensive', False)
		
		super().__init__(A, **kwargs)
		
		if skip_expensive:
			self._viz_settings.add('skip-expensive')
		
		self._viz_settings.add('gen-prior')
		if A.pull('force-viz', False):
			self._viz_settings.add('force')

	def _compute_fid(self, fid, generate_fn, name, out):
		
		statkey = f'{name}_fid_stats'
		
		if statkey not in out:
			out[statkey] = fid.compute_stats(generate_fn, name=name)
		stats = out[statkey]
		
		key = f'{name}_fid'
		
		if key not in out:
			try:
				dist = fid.compute_distance(stats)
			except AssertionError:
				print(f'Failed to compute the {name.capitalize()} FID')
			else:
				self.register_stats(key)
				self.mete(key, dist)
				out[key] = dist
				print(f'{name.capitalize()} FID: {dist:.2f}')
		
		dist = out[key]
		return dist
		
	def _evaluate(self, info, config, out=None):
		
		out = super()._evaluate(info, config, out=out)
		
		fid = config.pull('fid', None, ref=True)
		
		if fid is not None:
			
			dataset = info.get_dataset()
			
			try:
				base_stats = dataset.get_fid_stats(fid.dim, dataset.get_mode(), 'train')
			except MissingFIDStatsError as e:
				print(e)
			else:
				fid.set_baseline_stats(base_stats)
		
		if not config.pull('skip-rec-fid', False) and fid is not None:
			loader = info.get_loader(infinite=True)
			def _rec_gen(N):
				img = self._process_batch(loader.demand(N)).original
				return self(img)
			
			self._compute_fid(fid, generate_fn=_rec_gen, name='rec', out=out)
			
		
		return out

	def _visualize(self, info, records):
		
		settings = self._viz_settings
		
		if 'force' in settings:
			ori, rec = info.original, info.reconstruction
			
			B = ori.size(0)
			H, W = util.calc_tiling(ori.size(-1))
			
			info.original, info.reconstruction = ori.view(B, 1, H, W).sigmoid(), rec.view(B, 1, H, W).sigmoid()
		
		super()._visualize(info, records)
		
		x = info.original
		
		if self.get_mode() != 'train' and 'skip-expensive' not in self._viz_settings: # expensive visualizations
			
			n = 16
			steps = 20
			ntrav = 1
			
			if 'latent' in info and len(x.shape) > 2:
				q = info.latent
				if isinstance(info.latent, distrib.Distribution):
					q = q.loc
			
				fg, (lax, iax) = plt.subplots(2, figsize=(2*min(q.size(1)//20+1,3)+2,3))
				
				viz_util.viz_latent(q, figax=(fg, lax), )
				
				Q = q[:n]
				
				vecs = viz_util.get_traversal_vecs(Q, steps=steps,
				                                   mnmx=(Q.min(0)[0].unsqueeze(-1), Q.max(0)[0].unsqueeze(-1))).contiguous()
				# deltas = torch.diagonal(vecs, dim1=-3, dim2=-1)
				
				if 'force' in settings:
					def decode(q):
						B = q.size(0)
						r = self.decode(q)
						if isinstance(r, util.Distribution):
							r = r.bsample()
						r = r.view(B, 1, H, W).sigmoid()
						return r
				else:
					def decode(q):
						r = self.decode(q)
						if isinstance(r, util.Distribution):
							r = r.bsample()
						return r
						
				walks = viz_util.get_traversals(vecs, decode, device=self.device).cpu()
				diffs = viz_util.compute_diffs(walks)
				
				info.diffs = diffs
				
				viz_util.viz_interventions(diffs, figax=(fg, iax))
				

				# fig.tight_layout()
				border, between = 0.02, 0.01
				plt.subplots_adjust(wspace=between, hspace=between,
										left=5*border, right=1 - border, bottom=border, top=1 - border)
				
				records.log('figure', 'distrib', fg)
				
				full = walks[1:1+ntrav]
				del walks
				
				tH, tW = util.calc_tiling(full.size(1), prefer_tall=True)
				B, N, S, C, H, W = full.shape
				
				# if tH*H > 200: # limit total image size
				# 	pass
				
				full = full.view(B, tH, tW, S, C, H, W)
				full = full.permute(0, 3, 4, 1, 5, 2, 6).contiguous().view(B, S, C, tH * H, tW * W)
				
				records.log('video', 'traversals', full, fps=12)
			
			
			else:
				print('WARNING: visualizing traversals failed')
				

@fig.AutoModifier('hybrid')
class Hybrid(Autoencoder, Generative_AE):
	def __init__(self, A, **kwargs):

		viz_gen_hybrid = A.pull('viz-gen-hybrid', True)
	
		super().__init__(A, **kwargs)
		
		self.hybridize_groups = A.pull('hybridize-groups', False)
		
		self.register_buffer('_latent', None, persistent=True) # TODO: replace this with a proper replay buffer
		
		if viz_gen_hybrid:
			self._viz_settings.add('gen-hybrid')
	
	def _evaluate(self, info, config, out=None):
		
		out = super()._evaluate(info, config, out=out)
		
		if not config.pull('skip-hyb-fid', False):
		
			fid = config.pull('fid', None, ref=True, silent=True)
			if fid is not None:
				self._compute_fid(fid, self.generate_hybrid,
				                  name='hyb-grp' if self.hybridize_groups else 'hybrid', out=out)
	
		return out
	
	def _visualize(self, info, records):
		settings = self._viz_settings
		super()._visualize(info, records)
		
		B, C, *other = info.original.size()
		N = min(B, 8)
		
		if len(other) and ('gen-hybrid' in self._viz_settings or not self.training):
			viz_gen = self.generate_hybrid(2 * N).view(2*N, C, *other)
			if 'force' in settings:
				viz_gen = viz_gen.sigmoid()
			records.log('images', 'gen-hybrid', util.image_size_limiter(viz_gen))
	
	def _step(self, batch, out=None):
		out = super()._step(batch, out=out)
		if self.training and 'latent' in out:
			q = out.latent
			if isinstance(out.latent, distrib.Distribution):
				q = q.loc
			self._latent = q.detach()
		return out
	
	def hybridize(self, prior=None):
		if prior is None:
			prior = self._latent
		if self.hybridize_groups and hasattr(self.decoder, 'group_dims') and self.decoder.group_dims is not None:
			splits = self.decoder.group_dims
			groups = prior.split(splits, dim=1)
			qs = []
			for group in groups:
				qs.append(group[torch.randperm(len(group),device=group.device)])
			return torch.cat(qs, 1)
		return util.shuffle_dim(prior)
	
	def sample_hybrid(self, N=None, prior=None):
		if N is None:
			return self.hybridize(prior)
		remainder = None
		if prior is None:
			assert self._latent is not None, 'No latent vectors provided'
			prior = self._latent
		B = prior.size(0)
		if N > B:
			remainder = self.sample_hybrid(N - B, prior=prior)
			N = B
		batch = self.hybridize(prior)
		if N < B:
			batch = batch[:N]
		if remainder is not None:
			batch = torch.cat([batch, remainder], 0)
		return batch
	
	def generate(self, N=1, prior=None):
		return self.generate_hybrid(N, prior=prior)
	
	def generate_hybrid(self, N=1, prior=None):
		if prior is None:
			prior = self.sample_hybrid(N=N, prior=prior)
		imgs = self.decode(prior)
		if isinstance(imgs, util.Distribution):
			imgs = imgs.bsample()
		return imgs

@fig.Component('hybrid')
class Hybrid_Autoencoder(Hybrid):
	pass

@fig.AutoModifier('prior')
class Prior(Autoencoder, Generative_AE):
	def __init__(self, A, **kwargs):
		
		viz_gen_prior = A.pull('viz-gen-prior', True)
		
		super().__init__(A, **kwargs)
		
		if viz_gen_prior:
			self._viz_settings.add('gen-prior')
	
	def _evaluate(self, info, config, out=None):
		
		out = super()._evaluate(info, config, out=out)
		
		fid = config.pull('fid', None, ref=True, silent=True)
		if fid is not None:
			self._compute_fid(fid, self.generate_prior, name='prior', out=out)
		
		return out
	
	def _visualize(self, info, records):
		settings = self._viz_settings
		super()._visualize(info, records)
		
		B, C, *other = info.original.size()
		N = min(B, 8)
		
		if len(other) and ('gen-prior' in self._viz_settings or not self.training):
			viz_gen = self.generate_prior(2 * N).view(2*N, C, *other)
			if 'force' in settings:
				viz_gen = viz_gen.sigmoid()
			records.log('images', 'gen-prior', util.image_size_limiter(viz_gen))
	
	def generate(self, N=1, prior=None):
		return self.generate_prior(N, prior=prior)
		
	def generate_prior(self, N=1, prior=None):
		if prior is None:
			prior = self.sample_prior(N)
		imgs = self.decode(prior)
		if isinstance(imgs, util.Distribution):
			imgs = imgs.bsample()
		return imgs


@fig.Component('vae')
class VAE(Prior, Variational_Autoencoder):
	pass

@fig.Component('wae')
class WAE(Prior, Wasserstein_Autoencoder):
	pass

@fig.Component('swae')
class Slice_WAE(WAE):
	def __init__(self, A, **kwargs):
		slices = A.pull('slices', '<>latent_dim')

		super().__init__(A, **kwargs)

		self.slices = slices
		self.register_hparam('slices', slices)

	def sample_slices(self, N=None): # sampled D dim unit vectors
		if N is None:
			N = self.slices

		return F.normalize(torch.randn(self.latent_dim, N, device=self.device), p=2, dim=0)

	def regularize(self, q, p=None):

		s = self.sample_slices() # D, S

		qd = q @ s
		qd = qd.sort(0)[0]

		if p is None:
			p = self.sample_prior(q.size(0))
		pd = p @ s
		pd = pd.sort(0)[0]

		return (qd - pd).abs().mean()