import os
os.environ['NUMEXPR_MAX_THREADS'] = '88'

from model import *
from logger import *
from data_set import *
from loss import ShiftReLU, SmoothK2Loss

import sys
import random
import numpy as np
from datasets import load_dataset

from torch.optim import AdamW
import torch.distributed as dist
from torch.nn.utils import clip_grad_norm_
from torch.utils.data import DataLoader, DistributedSampler

from transformers import AutoTokenizer
from transformers import get_linear_schedule_with_warmup

# we need this to import senteval
sys.path.insert(0, '../SentEval')
import senteval

# we use DDP to train our model
local_rank = int(os.environ['LOCAL_RANK'])
dist.init_process_group(backend='nccl')
torch.cuda.set_device(local_rank)
device = torch.device('cuda', local_rank)

# use dev for training
evaluation_mode = 'dev'
evaluation_tasks = ['STSBenchmark']
sent_eval_data_path = '../SentEval/data'

save_path = 'save'
model_path = '../../models/bert-base-uncased'


def set_seed(seed=777):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def main():
    set_seed()

    # {'train': ['gold_label', 'sentence1', 'sentence2']}
    train_data = load_dataset('csv', data_dir='../nli_data', 
                              data_files='nli_ps_012_clean.csv',
                              cache_dir='./cache/')
    
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    train_data_set = NLIDataSet(tokenizer=tokenizer, data_set=train_data['train'])

    train_sampler = DistributedSampler(train_data_set)
    train_data_loader = DataLoader(dataset=train_data_set, batch_size=BATCH_SIZE * dist.get_world_size(), sampler=train_sampler)

    model = Average_BERT(bert_path=model_path)

    layer_learning_rate = {}
    base_learning_rate = BERT_LEARNING_RATE

    for i in range(11, -1, -1):
        layer_learning_rate[f'.{str(i)}.'] = base_learning_rate
        base_learning_rate *= RATE_DECAY_FACTOR

    optimizer_grouped_parameters = []
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    parameters = [(name, parameter) for name, parameter in model.named_parameters() if parameter.requires_grad]

    for name, parameter in parameters:
        params = {
            'params': [parameter],
            'lr': BERT_LEARNING_RATE,
            'weight_decay': 0.0
            if any(item in name for item in no_decay)
            else ADAM_WEIGHT_DECAY
        }   

        for layer_name, learning_rate in layer_learning_rate.items():
            if layer_name in name:
                params['lr'] = learning_rate
                break

        optimizer_grouped_parameters.append(params)
    
    optimizer = AdamW(params=optimizer_grouped_parameters,
                      lr=BERT_LEARNING_RATE,
                      betas=(ADAM_BETA_1, ADAM_BETA_2),
                      eps=ADAM_EPSILON,
                      weight_decay=ADAM_WEIGHT_DECAY)
    
    model = model.to(device)
    model = nn.parallel.DistributedDataParallel(model, device_ids=[local_rank], output_device=local_rank)

    total_steps = len(train_data_set) * EPOCH // (BATCH_SIZE * dist.get_world_size())
    warm_up_steps = int(total_steps * WARM_UP_RATE)
    scheduler = get_linear_schedule_with_warmup(optimizer=optimizer, num_warmup_steps=warm_up_steps, num_training_steps=total_steps)

    if local_rank == 0:
        LOGGER.info(f'total steps: {total_steps}, with warm up steps: {warm_up_steps} and decay rate: {RATE_DECAY_FACTOR}')

    steps = 0
    best_score = 0
    current_steps = 0
    for e_i in range(EPOCH):
        dist.barrier()
        for batch in train_data_loader:
            model.train()

            encoded_1, encoded_2 = batch[:2]
            prediction = model(encoded_1, encoded_2)

            # special treatment for the left critical point
            mask = (prediction >= 0).type(prediction.dtype)
            prediction = prediction * mask

            label = torch.FloatTensor(list(map(int, batch[-1]))).cuda()
            label = label.reshape(label.shape[0], 1)

            loss_function = SmoothK2Loss(threshold=0.25, k=2)
            loss = loss_function(prediction, label)
            loss.backward()

            clip_grad_norm_(parameters=model.parameters(), max_norm=GRADIENT_CLIPPING)
            optimizer.step()
            scheduler.step()
            model.zero_grad()

            dist.barrier()
            current_steps += dist.get_world_size()
            steps += dist.get_world_size()

            if steps < EVALUATION_PER_STEP:
                continue

            steps = 0
            model.eval()

            def prepare(params, samples):
                params.max_length = MAX_SEQUENCE_LENGTH
                return

            def batcher(params, batch):
                # Handle rare token encoding issues in the dataset
                if len(batch) >= 1 and len(batch[0]) >= 1 and isinstance(batch[0][0], bytes):
                    batch = [[word.decode('utf-8') for word in sentence] for sentence in batch]

                # batch is divided by token. we need to form sentences           
                sentences = [' '.join(sentence) for sentence in batch]

                batch = tokenizer.batch_encode_plus(
                    sentences,
                    padding='max_length',
                    truncation=True,
                    max_length=params.max_length,
                    return_tensors='pt',
                )

                for j in batch: 
                    batch[j] = batch[j].to(device)

                with torch.no_grad():
                    outputs = model.module.text2embedding(batch)

                return outputs.cpu()
            
            if evaluation_mode in ['dev', 'fasttest']:
                params = {
                    'task_path': sent_eval_data_path,
                    'usepytorch': True,
                    'kfold': 5,
                    'classifier': {
                        'nhid': 0,
                        'optim': 'rmsprop',
                        'batch_size': 128,
                        'tenacity': 3,
                        'epoch_size': 2,
                    },
                }
            elif evaluation_mode == 'test':
                params = {
                    'task_path': sent_eval_data_path,
                    'usepytorch': True,
                    'kfold': 10,
                    'classifier': {
                        'nhid': 0,
                        'optim': 'adam',
                        'batch_size': 64,
                        'tenacity': 5,
                        'epoch_size': 4,
                    },
                }
            else:
                raise ValueError(f'unknown {evaluation_mode}') 
            
            if local_rank == 0:
                total_score = 0
                for task in evaluation_tasks:
                    se = senteval.engine.SE(params, batcher, prepare)
                    result = se.eval(task)
                    total_score += result['dev']['spearman'][0] * 100
                
                average_score = total_score / len(evaluation_tasks)
                LOGGER.info(f'epoch: {e_i}, steps: {current_steps}, proportion: {current_steps / total_steps}, score: {average_score}')
                
                if average_score > best_score:
                    best_score = average_score
                    LOGGER.info(f'new best model, epoch: {e_i}, score: {best_score} !!!')
                    torch.save({'model': model.module.state_dict(), 'epoch': e_i, 'score': best_score}, open(os.path.join(save_path, 'best_model.pth'), 'wb'))


if __name__ == '__main__':
    main()