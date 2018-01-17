import io_helpers

import random
import time
from collections import namedtuple

import tensorboard_logger
import torch
import torch.nn

from torch.autograd import Variable

TRAIN_LOSS = 'train_loss'
VAL_LOSS = 'val_loss'
EPOCH_DURATION_SEC = 'epoch_duration_sec'
EXAMPLES_PER_SEC = 'examples_per_sec'

TrainSettings = namedtuple('TrainSettings', ['loss', 'epochs'])
Learner = namedtuple('Learner', ['net', 'optimizer', 'lr_scheduler'])

class SingleLabelLoss(torch.nn.Module):
  """Unwraps labels and predictions for a common case of only one prediction."""

  def __init__(self, base_loss):
    super(SingleLabelLoss, self).__init__()
    self.base_loss = base_loss
  
  def forward(self, predicted, labels):
    assert len(predicted) == 1
    assert len(labels) == 1
    return self.base_loss.forward(predicted[0], labels[0])  

class PowerLoss(torch.nn.Module):
  def __init__(self, p):
    super(PowerLoss, self).__init__()
    self.p = p
  
  def forward(self, predicted, labels):
    diff = torch.add(predicted, torch.neg(labels))
    loss_per_example = torch.pow(torch.abs(diff), self.p)
    for d in range(len(loss_per_example.shape) - 1, 0, -1):
      loss_per_example = torch.mean(loss_per_example, dim=d)
    return loss_per_example

def TrainLogEventToString(event):
  return 'loss %g;  val loss: %g;  %0.2f sec/epoch; %0.2f examples/sec' % (
      event[TRAIN_LOSS],
      event[VAL_LOSS],
      event[EPOCH_DURATION_SEC],
      event[EXAMPLES_PER_SEC])

def AverageLosses(total_losses, total_examples):
  return [
      x / y if y > 0 else float('inf')
      for x, y in zip(total_losses, total_examples)]

def DataBatchToVariables(batch, num_inputs, num_labels, cuda_device_id):
  assert len(batch) == num_inputs + num_labels
  input_vars = [Variable(x, requires_grad=False).cuda(cuda_device_id) for x in batch[:num_inputs]]
  label_vars = [Variable(x, requires_grad=False).cuda(cuda_device_id)
      for x in batch[num_inputs:(num_inputs + num_labels)]]
  return input_vars, label_vars

def TrainModels(
    learners,
    train_loader,
    val_loader,
    train_settings,
    out_dir,
    cuda_device_id=0,
    batch_use_prob=1.0,
    print_log=True,
    log_dir=''):
  logger = None
  if log_dir != '':
    logger = tensorboard_logger.Logger(log_dir, flush_secs=5)

  train_log = []
  min_validation_losses = [float('inf') for _ in learners]
  min_validation_loss = float('inf')
  num_inputs = len(learners[0].net.InputNames())
  num_labels = len(learners[0].net.LabelNames())
  for epoch in range(train_settings.epochs):
    running_losses = [0.0 for _ in learners]
    train_examples_per_net = [0 for _ in learners]
    for learner in learners:
      learner.optimizer.zero_grad()

    epoch_start_time = time.time()
    for training_batch in train_loader:
      input_vars, label_vars = DataBatchToVariables(
          training_batch, num_inputs, num_labels, cuda_device_id)

      for net_idx, learner in enumerate(learners):
        if random.uniform(0.0, 1.0) < batch_use_prob:
          # forward + backward + optimize
          outputs = learner.net(input_vars)
          loss_value_per_example = train_settings.loss(outputs, label_vars)
          # TODO take weights into account here.
          loss_value = torch.mean(loss_value_per_example)
          loss_value.backward()
          learner.optimizer.step()
          learner.optimizer.zero_grad()

          # TODO update weights using loss_value_per_example

          # Accumulate statistics
          batch_size = input_vars[0].size()[0]
          train_examples_per_net[net_idx] += batch_size
          running_losses[net_idx] += loss_value.data[0] * batch_size
    epoch_end_time = time.time()

    epoch_duration = epoch_end_time - epoch_start_time
    examples_per_sec = sum(train_examples_per_net) / epoch_duration
    avg_losses = AverageLosses(running_losses, train_examples_per_net)
    avg_loss = sum(running_losses) / sum(train_examples_per_net)

    validation_total_losses = [0.0 for _ in learners]
    validation_examples = [0 for _ in learners]

    for learner in learners:
      learner.net.eval()

    for val_batch in val_loader:
      input_vars, label_vars = DataBatchToVariables(
          val_batch, num_inputs, num_labels, cuda_device_id)

      for net_idx, learner in enumerate(learners):
        outputs = learner.net(input_vars)
        loss_value_per_example = train_settings.loss(outputs, label_vars)
        loss_value = torch.mean(loss_value_per_example)

        batch_size = input_vars[0].size()[0]
        validation_examples[net_idx] += batch_size
        validation_total_losses[net_idx] += loss_value.data[0] * batch_size
    
    for learner in learners:
      learner.net.train()
    
    validation_avg_losses = AverageLosses(
        validation_total_losses, validation_examples)
    validation_avg_loss = (
        sum(validation_total_losses) / sum(validation_examples))

    epoch_metrics = {
      TRAIN_LOSS: avg_loss,
      VAL_LOSS: validation_avg_loss,
      EPOCH_DURATION_SEC: epoch_duration,
      EXAMPLES_PER_SEC: examples_per_sec
    }
    train_log.append(epoch_metrics)

    val_improved_marker = ''
    if validation_avg_loss < min_validation_loss:
      val_improved_marker = ' ***'
      min_validation_loss = validation_avg_loss
    elif (validation_avg_loss * 0.9) < min_validation_loss:
      # Highlight epochs with validation loss almost as good as the current
      # best loss.
      val_improved_marker = ' *'

    for net_idx, learner in enumerate(learners):
      if learner.lr_scheduler is not None:
        learner.lr_scheduler.step(validation_avg_losses[net_idx])
      if validation_avg_losses[net_idx] < min_validation_losses[net_idx]:
        learner.net.cpu()
        torch.save(
            learner.net.state_dict(),
            io_helpers.ModelFileName(out_dir, net_idx, io_helpers.BEST))
        learner.net.cuda(cuda_device_id)
        min_validation_losses[net_idx] = validation_avg_losses[net_idx]
    
    # Maybe print metrics to screen.
    if print_log:
      print('Epoch %d;  %s%s' %
          (epoch, TrainLogEventToString(epoch_metrics), val_improved_marker))
    # Maybe log metrics to tensorboard.
    if log_dir != '':
      logger.log_value('train_loss', avg_loss, epoch)
      logger.log_value('val_loss', validation_avg_loss, epoch)
  
  for net_idx, learner in enumerate(learners):
    learner.net.cpu()
    torch.save(
        learner.net.state_dict(),
        io_helpers.ModelFileName(out_dir, net_idx, io_helpers.LAST))
    learner.net.cuda(cuda_device_id)

  return train_log