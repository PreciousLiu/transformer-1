#encoding: utf-8

# Difference: RNMT decoder concat attention and decoder layer output and directly classify with it, which makes the sharing parameters between classifier and embedding impossible, this implementation optionally reduce the concatenated dimension with another Linear transform followed by tanh like GlobalAttention

import torch
from torch import nn

from modules.base import *
from utils.sampler import SampleMax
from utils.base import all_done
from modules.rnncells import *

from utils.fmt.base import pad_id

from cnfg.ihyp import *

class FirstLayer(nn.Module):

	# isize: input size
	# osize: output size
	def __init__(self, isize, osize=None, dropout=0.0):

		super(FirstLayer, self).__init__()

		osize = isize if osize is None else osize

		self.net = LSTMCell4RNMT(isize, osize)
		self.init_hx = nn.Parameter(torch.zeros(1, osize))
		self.init_cx = nn.Parameter(torch.zeros(1, osize))

		self.drop = Dropout(dropout, inplace=False) if dropout > 0.0 else None

	# inputo: embedding of decoded translation (bsize, nquery, isize)
	# query_unit: single query to decode, used to support decoding for given step

	def forward(self, inputo, state=None, first_step=False):

		if state is None:
			hx, cx = prepare_initState(self.init_hx, self.init_cx, inputo.size(0))
			outs = []

			for _du in inputo.unbind(1):
				hx, cx = self.net(_du, (hx, cx))
				outs.append(hx)

			outs = torch.stack(outs, 1)

			if self.drop is not None:
				outs = self.drop(outs)

			return outs
		else:
			hx, cx = self.net(inputo, prepare_initState(self.init_hx, self.init_cx, inputo.size(0)) if first_step else state)

			out = hx if self.drop is None else self.drop(hx)

			return out, (hx, cx)

class DecoderLayer(nn.Module):

	# isize: input size
	# osize: output size
	def __init__(self, isize, osize=None, dropout=0.0, residual=True):

		super(DecoderLayer, self).__init__()

		osize = isize if osize is None else osize

		self.net = LSTMCell4RNMT(isize + osize, osize)
		self.init_hx = nn.Parameter(torch.zeros(1, osize))
		self.init_cx = nn.Parameter(torch.zeros(1, osize))

		self.drop = Dropout(dropout, inplace=False) if dropout > 0.0 else None

		self.residual = residual

	# inputo: embedding of decoded translation (bsize, nquery, isize)
	# query_unit: single query to decode, used to support decoding for given step

	def forward(self, inputo, attn, state=None, first_step=False):

		if state is None:
			hx, cx = prepare_initState(self.init_hx, self.init_cx, inputo.size(0))
			outs = []

			_inputo = torch.cat((inputo, attn), -1)

			for _du in _inputo.unbind(1):
				hx, cx = self.net(_du, (hx, cx))
				outs.append(hx)

			outs = torch.stack(outs, 1)

			if self.drop is not None:
				outs = self.drop(outs)

			return outs + inputo if self.residual else outs
		else:

			hx, cx = self.net(torch.cat((inputo, attn), -1), prepare_initState(self.init_hx, self.init_cx, inputo.size(0)) if first_step else state)

			out = hx if self.drop is None else self.drop(hx)

			return out + inputo if self.residual else out, (hx, cx)

class Decoder(nn.Module):

	# isize: size of word embedding
	# nwd: number of words
	# num_layer: number of layers
	# attn_drop: dropout for MultiHeadAttention
	# emb_w: weight for embedding. Use only when the encoder and decoder share a same dictionary
	# num_head: number of heads in MultiHeadAttention
	# xseql: maxmimum length of sequence
	# ahsize: number of hidden units for MultiHeadAttention
	# bindemb: bind embedding and classifier weight

	def __init__(self, isize, nwd, num_layer, dropout=0.0, attn_drop=0.0, emb_w=None, num_head=8, xseql=cache_len_default, ahsize=None, norm_output=True, bindemb=False, forbidden_index=None, projector=True):

		super(Decoder, self).__init__()

		_ahsize = isize if ahsize is None else ahsize

		self.drop = Dropout(dropout, inplace=True) if dropout > 0.0 else None

		self.xseql = xseql

		self.wemb = nn.Embedding(nwd, isize, padding_idx=0)
		if emb_w is not None:
			self.wemb.weight = emb_w

		self.flayer = FirstLayer(isize, osize=isize, dropout=dropout)

		self.attn = CrossAttn(isize, _ahsize, isize, num_head, dropout=attn_drop)

		self.nets = nn.ModuleList([DecoderLayer(isize, isize, dropout, i > 0) for i in range(num_layer - 1)])

		self.projector = Linear(isize, isize, bias=False) if projector else None

		self.classifier = Linear(isize * 2, nwd)#nn.Sequential(Linear(isize * 2, isize, bias=False), nn.Tanh(), Linear(isize, nwd))
		# be careful since this line of code is trying to share the weight of the wemb and the classifier, which may cause problems if torch.nn updates
		#if bindemb:
			#list(self.classifier.modules())[-1].weight = self.wemb.weight

		self.lsm = nn.LogSoftmax(-1)

		self.out_normer = nn.LayerNorm(isize, eps=ieps_ln_default, elementwise_affine=enable_ln_parameters) if norm_output else None

		self.fbl = None if forbidden_index is None else tuple(set(forbidden_index))

	# inpute: encoded representation from encoder (bsize, seql, isize)
	# inputo: decoded translation (bsize, nquery)
	# src_pad_mask: mask for given encoding source sentence (bsize, 1, seql), see Encoder, generated with:
	#	src_pad_mask = input.eq(0).unsqueeze(1)

	def forward(self, inpute, inputo, src_pad_mask=None):

		out = self.wemb(inputo)

		if self.drop is not None:
			out = self.drop(out)

		out = self.flayer(out)

		if self.projector:
			inpute = self.projector(inpute)

		attn = self.attn(out, inpute, src_pad_mask)

		# the following line of code is to mask <pad> for the decoder,
		# which I think is useless, since only <pad> may pay attention to previous <pad> tokens, whos loss will be omitted by the loss function.
		#_mask = torch.gt(_mask + inputo.eq(0).unsqueeze(1), 0)

		for net in self.nets:
			out = net(out, attn)

		if self.out_normer is not None:
			out = self.out_normer(out)

		out = self.lsm(self.classifier(torch.cat((out, attn), -1)))

		return out

	# inpute: encoded representation from encoder (bsize, seql, isize)
	# src_pad_mask: mask for given encoding source sentence (bsize, seql), see Encoder, get by:
	#	src_pad_mask = input.eq(0).unsqueeze(1)
	# beam_size: the beam size for beam search
	# max_len: maximum length to generate

	def decode(self, inpute, src_pad_mask, beam_size=1, max_len=512, length_penalty=0.0, fill_pad=False):

		return self.beam_decode(inpute, src_pad_mask, beam_size, max_len, length_penalty, fill_pad=fill_pad) if beam_size > 1 else self.greedy_decode(inpute, src_pad_mask, max_len, fill_pad=fill_pad)

	# inpute: encoded representation from encoder (bsize, seql, isize)
	# src_pad_mask: mask for given encoding source sentence (bsize, 1, seql), see Encoder, generated with:
	#	src_pad_mask = input.eq(0).unsqueeze(1)
	# max_len: maximum length to generate

	def greedy_decode(self, inpute, src_pad_mask=None, max_len=512, fill_pad=False, sample=False):

		bsize = inpute.size(0)

		out = self.get_sos_emb(inpute)

		# out: input to the decoder for the first step (bsize, 1, isize)

		if self.drop is not None:
			out = self.drop(out)

		out, statefl = self.flayer(out, "init", True)

		states = {}

		if self.projector:
			inpute = self.projector(inpute)

		attn = self.attn(out.unsqueeze(1), inpute, src_pad_mask).squeeze(1)

		for _tmp, net in enumerate(self.nets):
			out, _state = net(out, attn, "init", True)
			states[_tmp] = _state

		if self.out_normer is not None:
			out = self.out_normer(out)

		# out: (bsize, nwd)
		out = self.classifier(torch.cat((out, attn), -1))
		# wds: (bsize)
		wds = SampleMax(out.softmax(-1), dim=-1, keepdim=False) if sample else out.argmax(dim=-1)

		trans = [wds]

		# done_trans: (bsize)

		done_trans = wds.eq(2)

		for i in range(1, max_len):

			out = self.wemb(wds)

			if self.drop is not None:
				out = self.drop(out)

			out, statefl = self.flayer(out, statefl)

			attn = self.attn(out.unsqueeze(1), inpute, src_pad_mask).squeeze(1)

			for _tmp, net in enumerate(self.nets):
				out, _state = net(out, attn, states[_tmp])
				states[_tmp] = _state

			if self.out_normer is not None:
				out = self.out_normer(out)

			out = self.classifier(torch.cat((out, attn), -1))
			wds = SampleMax(out.softmax(-1), dim=-1, keepdim=False) if sample else out.argmax(dim=-1)

			trans.append(wds.masked_fill(done_trans, pad_id) if fill_pad else wds)

			done_trans = done_trans | wds.eq(2)
			if all_done(done_trans, bsize):
				break

		return torch.stack(trans, 1)

	# inpute: encoded representation from encoder (bsize, seql, isize)
	# src_pad_mask: mask for given encoding source sentence (bsize, 1, seql), see Encoder, generated with:
	#	src_pad_mask = input.eq(0).unsqueeze(1)
	# beam_size: beam size
	# max_len: maximum length to generate

	def beam_decode(self, inpute, src_pad_mask=None, beam_size=8, max_len=512, length_penalty=0.0, return_all=False, clip_beam=False, fill_pad=False):

		bsize, seql = inpute.size()[:2]

		beam_size2 = beam_size * beam_size
		bsizeb2 = bsize * beam_size2
		real_bsize = bsize * beam_size

		out = self.get_sos_emb(inpute)
		isize = out.size(-1)

		if length_penalty > 0.0:
			# lpv: length penalty vector for each beam (bsize * beam_size, 1)
			lpv = out.new_ones(real_bsize, 1)
			lpv_base = 6.0 ** length_penalty

		if self.drop is not None:
			out = self.drop(out)

		out, statefl = self.flayer(out, "init", True)
		statefl = torch.stack(statefl, -2)

		states = {}

		if self.projector:
			inpute = self.projector(inpute)

		attn = self.attn(out.unsqueeze(1), inpute, src_pad_mask).squeeze(1)

		for _tmp, net in enumerate(self.nets):
			out, _state = net(out, attn, "init", True)
			states[_tmp] = torch.stack(_state, -2)

		if self.out_normer is not None:
			out = self.out_normer(out)

		# out: (bsize, nwd)

		out = self.lsm(self.classifier(torch.cat((out, attn), -1)))

		# scores: (bsize, beam_size) => (bsize, beam_size)
		# wds: (bsize * beam_size)
		# trans: (bsize * beam_size, 1)

		scores, wds = out.topk(beam_size, dim=-1)
		sum_scores = scores
		wds = wds.view(real_bsize)
		trans = wds.unsqueeze(1)

		# done_trans: (bsize, beam_size)

		done_trans = wds.view(bsize, beam_size).eq(2)

		# inpute: (bsize, seql, isize) => (bsize * beam_size, seql, isize)

		inpute = inpute.repeat(1, beam_size, 1).view(real_bsize, seql, isize)

		# _src_pad_mask: (bsize, 1, seql) => (bsize * beam_size, 1, seql)

		_src_pad_mask = None if src_pad_mask is None else src_pad_mask.repeat(1, beam_size, 1).view(real_bsize, 1, seql)

		# states[i]: (bsize, 2, isize) => (bsize * beam_size, 2, isize)

		statefl = statefl.repeat(1, beam_size, 1).view(real_bsize, 2, isize)
		for key, value in states.items():
			states[key] = value.repeat(1, beam_size, 1).view(real_bsize, 2, isize)

		for step in range(1, max_len):

			out = self.wemb(wds)

			if self.drop is not None:
				out = self.drop(out)

			out, statefl = self.flayer(out, statefl.unbind(-2))
			statefl = torch.stack(statefl, -2)

			attn = self.attn(out.unsqueeze(1), inpute, _src_pad_mask).squeeze(1)

			for _tmp, net in enumerate(self.nets):
				out, _state = net(out, attn, states[_tmp].unbind(-2))
				states[_tmp] = torch.stack(_state, -2)

			if self.out_normer is not None:
				out = self.out_normer(out)

			# out: (bsize, beam_size, nwd)

			out = self.lsm(self.classifier(torch.cat((out, attn), -1))).view(bsize, beam_size, -1)

			# find the top k ** 2 candidates and calculate route scores for them
			# _scores: (bsize, beam_size, beam_size)
			# done_trans: (bsize, beam_size)
			# scores: (bsize, beam_size)
			# _wds: (bsize, beam_size, beam_size)
			# mask_from_done_trans: (bsize, beam_size) => (bsize, beam_size * beam_size)
			# added_scores: (bsize, 1, beam_size) => (bsize, beam_size, beam_size)

			_scores, _wds = out.topk(beam_size, dim=-1)
			_scores = (_scores.masked_fill(done_trans.unsqueeze(2).expand(bsize, beam_size, beam_size), 0.0) + sum_scores.unsqueeze(2).expand(bsize, beam_size, beam_size))

			if length_penalty > 0.0:
				lpv = lpv.masked_fill(~done_trans.view(real_bsize, 1), ((step + 6.0) ** length_penalty) / lpv_base)

			# clip from k ** 2 candidate and remain the top-k for each path
			# scores: (bsize, beam_size * beam_size) => (bsize, beam_size)
			# _inds: indexes for the top-k candidate (bsize, beam_size)

			if clip_beam and (length_penalty > 0.0):
				scores, _inds = (_scores.view(real_bsize, beam_size) / lpv.expand(real_bsize, beam_size)).view(bsize, beam_size2).topk(beam_size, dim=-1)
				_tinds = (_inds + torch.arange(0, bsizeb2, beam_size2, dtype=_inds.dtype, device=_inds.device).unsqueeze(1).expand_as(_inds)).view(real_bsize)
				sum_scores = _scores.view(bsizeb2).index_select(0, _tinds).view(bsize, beam_size)
			else:
				scores, _inds = _scores.view(bsize, beam_size2).topk(beam_size, dim=-1)
				_tinds = (_inds + torch.arange(0, bsizeb2, beam_size2, dtype=_inds.dtype, device=_inds.device).unsqueeze(1).expand_as(_inds)).view(real_bsize)
				sum_scores = scores

			# select the top-k candidate with higher route score and update translation record
			# wds: (bsize, beam_size, beam_size) => (bsize * beam_size)

			wds = _wds.view(bsizeb2).index_select(0, _tinds)

			# reduces indexes in _inds from (beam_size ** 2) to beam_size
			# thus the fore path of the top-k candidate is pointed out
			# _inds: indexes for the top-k candidate (bsize, beam_size)

			_inds = (_inds // beam_size + torch.arange(0, real_bsize, beam_size, dtype=_inds.dtype, device=_inds.device).unsqueeze(1).expand_as(_inds)).view(real_bsize)

			# select the corresponding translation history for the top-k candidate and update translation records
			# trans: (bsize * beam_size, nquery) => (bsize * beam_size, nquery + 1)

			trans = torch.cat((trans.index_select(0, _inds), (wds.masked_fill(done_trans.view(real_bsize), pad_id) if fill_pad else wds).unsqueeze(1)), 1)

			done_trans = (done_trans.view(real_bsize).index_select(0, _inds) & wds.eq(2)).view(bsize, beam_size)

			# check early stop for beam search
			# done_trans: (bsize, beam_size)
			# scores: (bsize, beam_size)

			_done = False
			if length_penalty > 0.0:
				lpv = lpv.index_select(0, _inds)
			elif (not return_all) and all_done(done_trans.select(1, 0), bsize):
				_done = True

			# check beam states(done or not)

			if _done or all_done(done_trans, real_bsize):
				break

			# update the corresponding hidden states
			# states[i]: (bsize * beam_size, 2, isize)
			# _inds: (bsize, beam_size) => (bsize * beam_size)

			statefl = statefl.index_select(0, _inds)
			for key, value in states.items():
				states[key] = value.index_select(0, _inds)

		# if length penalty is only applied in the last step, apply length penalty
		if (not clip_beam) and (length_penalty > 0.0):
			scores = scores / lpv.view(bsize, beam_size)
			scores, _inds = scores.topk(beam_size, dim=-1)
			_inds = (_inds + torch.arange(0, real_bsize, beam_size, dtype=_inds.dtype, device=_inds.device).unsqueeze(1).expand_as(_inds)).view(real_bsize)
			trans = trans.view(real_bsize, -1).index_select(0, _inds).view(bsize, beam_size, -1)

		if return_all:

			return trans, scores
		else:

			return trans.view(bsize, beam_size, -1).select(1, 0)

	# inpute: encoded representation from encoder (bsize, seql, isize)

	def get_sos_emb(self, inpute):

		bsize = inpute.size(0)

		return self.wemb.weight[1].view(1, -1).expand(bsize, -1)

	def fix_init(self):

		self.fix_load()
		with torch.no_grad():
			self.wemb.weight[pad_id].zero_()
			self.classifier.weight[pad_id].zero_()

	def fix_load(self):

		if self.fbl is not None:
			with torch.no_grad():
				#list(self.classifier.modules())[-1].bias.index_fill_(0, torch.tensor(self.fbl, dtype=torch.long, device=self.classifier.bias.device), -inf_default)
				self.classifier.bias.index_fill_(0, torch.tensor(self.fbl, dtype=torch.long, device=self.classifier.bias.device), -inf_default)
