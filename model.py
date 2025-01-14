import torch
import torch.nn as nn
from torch.autograd import Variable
from math import sqrt

class RNNModel(nn.Module):
    """Container module with an encoder, a recurrent module, and a decoder."""

    def __init__(self, rnn_type, ntoken, emsize, nhid, nlayers, dropout=0.5, 
                    tie_weights=False, cutoff = None, adaptive_softmax=False):
        super(RNNModel, self).__init__()


        self.drop = nn.Dropout(dropout)
        self.encoder = nn.Embedding(ntoken, emsize)
        
        if rnn_type in ['LSTM', 'GRU']:
            self.rnn = getattr(nn, rnn_type)(emsize, nhid, nlayers, dropout=dropout)
        else:
            try:
                nonlinearity = {'RNN_TANH': 'tanh', 'RNN_RELU': 'relu'}[rnn_type]
            except KeyError:
                raise ValueError( """An invalid option for `--model` was supplied,
                                 options are ['LSTM', 'GRU', 'RNN_TANH' or 'RNN_RELU']""")
            self.rnn = nn.RNN(emsize, nhid, nlayers, nonlinearity=nonlinearity, dropout=dropout)
        
        if adaptive_softmax:
            self.decoder = AdaptiveSoftmax(nhid, [*cutoff, ntoken]) 
        else:
            self.decoder = nn.Linear(nhid, ntoken)

        # Optionally tie weights as in:
        # "Using the Output Embedding to Improve Language Models" (Press & Wolf 2016)
        # https://arxiv.org/abs/1608.05859
        # and
        # "Tying Word Vectors and Word Classifiers: A Loss Framework for Language Modeling" (Inan et al. 2016)
        # https://arxiv.org/abs/1611.01462
        if tie_weights:
            if nhid != emsize:
                raise ValueError('When using the tied flag, nhid must be equal to emsize')
            self.decoder.weight = self.encoder.weight

        self.rnn_type = rnn_type
        self.nhid = nhid
        self.nlayers = nlayers
        self.adaptive_softmax = adaptive_softmax
        self.tie_weights = tie_weights
        self.init_weights()

    def init_weights(self):
        nn.init.xavier_uniform(self.encoder.weight.data)

        if not self.adaptive_softmax:
            self.decoder.bias.data.fill_(0)

            if not self.tie_weights:
                nn.init.xavier_uniform(self.decoder.weight.data)




    def forward(self, input, hidden, target=None):

        emb = self.drop(self.encoder(input))
        output, hidden = self.rnn(emb, hidden)
        output = self.drop(output)
        

        if self.adaptive_softmax:
            self.decoder.set_target(target.data) 
            decoded = self.decoder(output.view(output.contiguous().size(0)*output.size(1), output.size(2)))
            return decoded, hidden

       
        decoded = self.decoder(output.view(output.size(0)*output.size(1), output.size(2)))
        return decoded.view(output.size(0), output.size(1), decoded.size(1)), hidden       
    

    def log_prob(self, input, hidden):
        # adasoft requirement
        emb = self.encoder(input)
        output, hidden = self.rnn(emb, hidden)
        decoded = self.decoder.log_prob(output.contiguous() \
                .view(output.size(0) * output.size(1), output.size(2)))

        return decoded, hidden


    def init_hidden(self, bsz):
        weight = next(self.parameters()).data
        if self.rnn_type == 'LSTM':
            return (Variable(weight.new(self.nlayers, bsz, self.nhid).zero_()),
                    Variable(weight.new(self.nlayers, bsz, self.nhid).zero_()))
        else:
            return Variable(weight.new(self.nlayers, bsz, self.nhid).zero_())




# AdaptiveSoftmax and AdaptiveLoss are based on  
# "Efficient softmax approximation for GPUs" (http://arxiv.org/abs/1609.04309).
# and reproduced in python by Kim Seonghyeon here:
# https://github.com/rosinality/adaptive-softmax-pytorch/
class AdaptiveSoftmax(nn.Module):
    def __init__(self, nhid, cutoff):
        '''
        nhid  (int) is the input_size
        cutoff (list) determines size of each branch
        '''

        super().__init__()

        self.nhid = nhid
        self.cutoff = cutoff
        self.output_size = cutoff[0] + len(cutoff) - 1

        self.head = nn.Linear(nhid, self.output_size)
        self.tail = nn.ModuleList()

        for i in range(len(cutoff) - 1):
            seq = nn.Sequential(
                nn.Linear(nhid, nhid // 4 ** i, False),
                nn.Linear(nhid // 4 ** i, cutoff[i + 1] - cutoff[i], False)
            )

            self.tail.append(seq)

    def reset(self):
        
        nn.init.xavier_normal(self.head.weight)
        for tail in self.tail:
            nn.init.xavier_normal(tail[0].weight)
            nn.init.xavier_normal(tail[1].weight)
            

    def set_target(self, target):
        
        self.id = []
        for i in range(len(self.cutoff) - 1):
            mask = target.ge(self.cutoff[i]).mul(target.lt(self.cutoff[i + 1]))

            if mask.sum() > 0:
                self.id.append(Variable(mask.float().nonzero().squeeze(1)))
            else:
                print("set_target no data in vocabulary bin")
                self.id.append(None)
                

    def forward(self, input):

        output = [self.head(input)]
        for i in range(len(self.id)):

            if self.id[i] is not None:
                output.append(self.tail[i](input.index_select(0, self.id[i])))

            else:
                output.append(None)

        return output

    def log_prob(self, input):
        lsm = nn.LogSoftmax()

        head_out = self.head(input)

        batch_size = head_out.size(0)
        prob = torch.zeros(batch_size, self.cutoff[-1])

        lsm_head = lsm(head_out)
        prob.narrow(1, 0, self.output_size).add_(lsm_head.narrow(1, 0, self.output_size).data)

        for i in range(len(self.tail)):
            pos = self.cutoff[i]
            i_size = self.cutoff[i + 1] - pos
            buffer = lsm_head.narrow(1, self.cutoff[0] + i, 1)
            buffer = buffer.expand(batch_size, i_size)
            lsm_tail = lsm(self.tail[i](input))
            prob.narrow(1, pos, i_size).copy_(buffer.data).add_(lsm_tail.data)

        return prob

class AdaptiveLoss(nn.Module):
    def __init__(self, cutoff):
        super().__init__()

        self.cutoff = cutoff
        self.criterion = nn.CrossEntropyLoss(size_average=False)

    def remap_target(self, target):
        '''map targets to len(self.cutoff) vocab bins'''

        # target is a tensor length bptt * batch_size
        new_target = [target.clone()]

        
        for i in range(len(self.cutoff) - 1):
            # collect targets that fit into each vocab bin.
            mask = target.ge(self.cutoff[i]).mul(target.lt(self.cutoff[i + 1]))
            new_target[0][mask] = self.cutoff[0] + i

            if mask.sum() > 0:
                # the classes feed to the secondary soft-maxes need
                # to be in range 0 to C - 1
                new_target.append(target[mask].add(-self.cutoff[i]))

            else:
                new_target.append(None)

        return new_target

    def forward(self, input, target):
        batch_size = input[0].size(0)
        target = self.remap_target(target.data)

        output = 0.0

        for i in range(len(input)):
            if input[i] is not None:
                assert(target[i].min() >= 0 and target[i].max() <= input[i].size(1))
                try:
                    output += self.criterion(input[i], Variable(target[i]))
                except:
                    import pdb; pdb.set_trace()
        output /= batch_size

        return output