#encoding: utf-8

from torch import nn
from modules import *
from math import sqrt
from transformer.Encoder import EncoderLayer

# vocabulary:
#	<pad>:0
#	<unk>:1
#	<eos>:2
#	<sos>:3
#	...
# for the classier of the decoder, <sos> is omitted

class Encoder(nn.Module):

	# isize: size of word embedding
	# nwd: number of words
	# num_layer: number of encoder layers
	# fhsize: number of hidden units for PositionwiseFeedForward
	# attn_drop: dropout for MultiHeadAttention
	# num_head: number of heads in MultiHeadAttention
	# xseql: maxmimum length of sequence
	# ahsize: number of hidden units for MultiHeadAttention

	def __init__(self, isize, nwd, num_layer, fhsize=None, dropout=0.0, attn_drop=0.0, num_head=8, xseql=512, ahsize=None, norm_output=True):

		super(Encoder, self).__init__()

		_ahsize = isize if ahsize is None else ahsize

		_fhsize = _ahsize * 4 if fhsize is None else fhsize

		self.num_layer = num_layer

		self.drop = nn.Dropout(dropout, inplace=True) if dropout > 0.0 else None

		self.wemb = nn.Embedding(nwd, isize, padding_idx=0)

		self.pemb = CoordinateEmb(isize, xseql, num_layer, 0, 0)
		self.net = EncoderLayer(isize, _fhsize, dropout, attn_drop, num_head, _ahsize)
		self.halter = nn.Sequential(Scorer(isize), nn.Sigmoid())

		self.out_normer = nn.LayerNorm(isize, eps=1e-06) if norm_output else None

		self.act_loss = ACT_Loss()

	# inputs: (bsize, seql)
	# mask: (bsize, 1, seql), generated with:
	#	mask = inputs.eq(0).unsqueeze(1)

	def forward(self, inputs, mask=None):

		bsize, seql = inputs.size()
		out = self.wemb(inputs)

		if self.drop is not None:
			out = self.drop(out)

		outs = []
		wl = []
		sum_w = None
		done = None
		act_loss = []
		for i in range(self.num_layer):

			# out: (bsize, seql, isize)
			out = self.net(out + self.pemb(out, i, expand=False), mask)
			outs.append(out)
			# w: (bsize, seql, 1)
			w = self.halter(out)
			max_w = w.new_ones(1) if sum_w is None else 1.0 - sum_w
			w = torch.min(max_w, w)
			wl.append(w)
			# sum_w, remainv, done: (bsize, seql, 1)
			sum_w = w if sum_w is None else sum_w + w
			remainv = 1.0 - sum_w
			done = torch.lt(remainv, 0.01) if done is None else torch.gt(done + torch.lt(remainv, 0.01), 0)
			if self.training:
				act_loss.append(w.new_full(w.size(), -1.0).masked_fill(done, 0.0))
			if done.sum() == done.numel():
				break

		wl[-1] += remainv
		# out: (bsize, seql, isize, nlayer) => (bsize, seql, isize)
		# w: (bsize, seql, nlayer, 1)
		out = torch.stack(outs, dim=-1)
		w = torch.stack(wl, dim=-2)
		if self.training:
			act_loss = torch.stack(act_loss, dim=-2)
			loss_act = self.act_loss(w, act_loss, remainv)
		else:
			loss_act = None
		out = torch.matmul(out, w).squeeze(-1)

		if self.out_normer is not None:
			out = self.out_normer(out)

		if loss_act is None:
			return out
		else:
			return out, loss_act