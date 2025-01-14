# coding: utf-8
import argparse
import time
import math
import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.optim as optim
import data
import model
from model import AdaptiveLoss

parser = argparse.ArgumentParser(description='PTB RNN/LSTM Language Model: Main Function')
parser.add_argument('--data', type=str, default='./data/ptb',
                    help='location of the data corpus')
parser.add_argument('--model', type=str, default='LSTM',
                    help='type of recurrent net (RNN_TANH, RNN_RELU, LSTM, GRU)')
parser.add_argument('--emsize', type=int, default=100,
                    help='size of word embeddings')
parser.add_argument('--nhid', type=int, default=128,
                    help='number of hidden units per layer')
parser.add_argument('--nlayers', type=int, default=1,
                    help='number of layers')
parser.add_argument('--lr', type=float, default=20,
                    help='initial learning rate')
parser.add_argument('--clip', type=float, default=0.25,
                    help='gradient clipping')
parser.add_argument('--epochs', type=int, default=5,
                    help='upper epoch limit')
parser.add_argument('--batch_size', type=int, default=32, metavar='N',
                    help='batch size')
parser.add_argument('--bptt', type=int, default=35,
                    help='sequence length')
parser.add_argument('--bptt_multiplier', type=float, default=1,
                    help='factor to increase sequence length')
parser.add_argument('--dropout', type=float, default=0.2,
                    help='dropout applied to layers (0 = no dropout)')
parser.add_argument('--change_dropout', type=int, default=10000,
                    help='after n epochs increase dropout rate')
parser.add_argument('--tied', action='store_true',
                    help='tie the word embedding and softmax weights')
parser.add_argument('--seed', type=int, default=1111,
                    help='random seed')
parser.add_argument('--log_interval', type=int, default=200, metavar='N',
                    help='report interval')
parser.add_argument('--save', type=str,  default='model.pt',
                    help='path to save the final model')
parser.add_argument('--adasoft', action='store_true',
                    help='use adaptive softmax')
parser.add_argument('--cutoff', type=str, default="500,2000")
parser.add_argument('--adam', action='store_true',
                    help='use adam')

args = parser.parse_args()
meta_data = vars(args)
meta_data["train_time"] = 0

adam = args.adam
# Set the random seed manually for reproducibility.
torch.manual_seed(args.seed)

###############################################################################
# Load data
###############################################################################

#corpus = data.Corpus(args.data)
corpus = data.Corpus2(args.data)
#import pdb; pdb.set_trace()

# Starting from sequential data, batchify arranges the dataset into columns.
# For instance, with the alphabet as the sequence and batch size 4, we'd get
# ┌ a g m s ┐
# │ b h n t │
# │ c i o u │
# │ d j p v │
# │ e k q w │
# └ f l r x ┘.
# These columns are treated as independent by the model, which means that the
# dependence of e. g. 'g' on 'f' can not be learned, but allows more efficient
# batch processing.

def batchify(data, bsz):
    # Work out how cleanly we can divide the dataset into bsz parts.
    nbatch = data.size(0) // bsz
    # Trim off any extra elements that wouldn't cleanly fit (remainders).
    data = data.narrow(0, 0, nbatch * bsz)
    # Evenly divide the data across the bsz batches.
    data = data.view(bsz, -1).t().contiguous()
    return data

eval_batch_size = 10
train_data = batchify(corpus.train, args.batch_size)
val_data = batchify(corpus.valid, eval_batch_size)
test_data = batchify(corpus.test, eval_batch_size)
###############################################################################
# Build the model
###############################################################################
ntokens = len(corpus.dictionary)
print(args, ", ntokens = {}".format(ntokens))

if args.adasoft:
    cutoff = [int(c) for c in args.cutoff.split(",")]
    model = model.RNNModel(args.model, ntokens, args.emsize, args.nhid, args.nlayers, \
                            args.dropout, args.tied, cutoff, args.adasoft)
    criterion = AdaptiveLoss([*cutoff, ntokens + 1])

else:
    model = model.RNNModel(args.model, ntokens, args.emsize, args.nhid, args.nlayers, \
                            args.dropout, args.tied)
    criterion = nn.CrossEntropyLoss()



###############################################################################
# Training code
###############################################################################

def repackage_hidden(h):
    """Wraps hidden states in new Variables, to detach them from their history."""
    if type(h) == Variable:
        return Variable(h.data)
    else:
        return tuple(repackage_hidden(v) for v in h)


# get_batch subdivides the source data into chunks of length args.bptt.
# If source is equal to the example output of the batchify function, with
# a bptt-limit of 2, we'd get the following two Variables for i = 0:
# ┌ a g m s ┐ ┌ b h n t ┐
# └ b h n t ┘ └ c i o u ┘
# Note that despite the name of the function, the subdivison of data is not
# done along the batch dimension (i.e. dimension 1), since that was handled
# by the batchify function. The chunks are along dimension 0, corresponding
# to the seq_len dimension in the LSTM.

def get_batch(source, i, evaluation=False):
    seq_len = min(args.bptt, len(source) - 1 - i)
    data = Variable(source[i:i+seq_len], volatile=evaluation)
    target = Variable(source[i+1:i+1+seq_len].view(-1))
    return data, target


def evaluate(data_source):
    # Turn on evaluation mode which disables dropout.
    model.eval()
    total_loss = 0
    ntokens = len(corpus.dictionary)
    hidden = model.init_hidden(eval_batch_size)

    if args.adasoft:
        eval_criterion = nn.NLLLoss()
    else:
        eval_criterion = nn.CrossEntropyLoss()


    for i in range(0, data_source.size(0) - 1, args.bptt):
        data, targets = get_batch(data_source, i, evaluation=True)
        

        if args.adasoft:
            output, hidden = model.log_prob(data, hidden)
            output = Variable(output)
        else:
            output, hidden = model(data, hidden)
            output = output.view(-1, ntokens)

        total_loss +=  len(data) * eval_criterion(output, targets).data
        hidden = repackage_hidden(hidden)
    
    return total_loss[0] / len(data_source)


def train():
    # torch.set_num_threads(1)
    # Turn on training mode which enables dropout.
    model.train()
    total_loss = 0
   
    ntokens = len(corpus.dictionary)
    hidden = model.init_hidden(args.batch_size)

    start_time = time.time()
    for batch, i in enumerate(range(0, train_data.size(0) - 1, args.bptt)):
        data, targets = get_batch(train_data, i)
        # Starting each batch, we detach the hidden state from how it was previously produced.
        # If we didn't, the model would try backpropagating all the way to start of the dataset.
        hidden = repackage_hidden(hidden)
        model.zero_grad()
        
        if args.adasoft:
            output, hidden = model(data, hidden, targets)
            loss = criterion(output, targets)

        else:
            output, hidden = model(data, hidden) 
            loss = criterion(output.view(-1, ntokens), targets)

        loss.backward()

        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        torch.nn.utils.clip_grad_norm(model.parameters(), args.clip)
        
        if adam:
            optimizer.step()
        else:
            for p in model.parameters():
                p.data.add_(-lr, p.grad.data)
        
        total_loss += loss.data

        if batch % args.log_interval == 0 and batch > 0:
            cur_loss = total_loss[0] / args.log_interval
            elapsed = time.time() - start_time
            print('| epoch {:3d} | {:5d}/{:5d} batches | lr {:02.2f} | ms/batch {:5.2f} | '
                    'loss {:5.2f} | perplexity {:8.2f}'.format(
                epoch, batch, len(train_data) // args.bptt, lr,
                elapsed * 1000 / args.log_interval, cur_loss, math.exp(cur_loss)))
            total_loss = 0
            start_time = time.time()

# Loop over epochs.
lr = args.lr
best_val_loss = None
# At any point you can hit Ctrl + C to break out of training early.
try:
    if adam:
        optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=0)
    
    for epoch in range(1, args.epochs+1):

        # if args.change_dropout == epoch:
        #     args.dropout += .2
        #     print("dropout is now: ", args.dropout)
        
        if args.bptt_multiplier != 1:
            print("bptt is: ", args.bptt)
        
        epoch_start_time = time.time()
        train()

        val_loss = evaluate(val_data)
        args.bptt = int(args.bptt_multiplier * args.bptt)
        train_time = time.time() - epoch_start_time
        print('-' * 89)
        print('| end of epoch {:3d} | time: {:5.2f}s | valid loss {:5.2f} | '
                'valid perplexity {:8.2f}'.format(epoch, (train_time),
                                           val_loss, math.exp(val_loss)))
        print('-' * 89)

        meta_data["train_time"] += train_time
        meta_data["val_ppl_" + str(epoch)] = math.exp(val_loss)

        # Save the model if the validation loss is the best we've seen so far.
        if not best_val_loss or val_loss < best_val_loss:
            with open(args.save, 'wb') as f:
                torch.save(model, f)
            best_val_loss = val_loss
        else:
            # Anneal the learning rate if no improvement has been seen in the validation dataset.
            lr /= 4.0
except KeyboardInterrupt:
    print('-' * 89)
    print('Exiting from training early')



# Load the best saved model.
with open(args.save, 'rb') as f:
    model = torch.load(f)

# Run on test data.
test_loss = evaluate(test_data)
print('=' * 89)
print('| End of training | training time: {:5.2f}s |test loss {:5.2f} | test perplexity {:8.2f}'.format(meta_data['train_time'],
    test_loss, math.exp(test_loss)))
print('=' * 89)

meta_data["test_ppl"] = math.exp(test_loss)
print(meta_data)

import pandas as pd
if args.save != 'model.pt':
    name = args.save.replace(".pt","")
else:
    name = str(time.time())
pd.DataFrame(meta_data, index=[name]).to_csv(name + ".csv")


#return meta_data



