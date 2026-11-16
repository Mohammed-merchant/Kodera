import torch
print(f"Torch version: {torch.__version__}")
print(f'CUDA available: {torch.cuda.is_available()}')
device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
print(f'the device is {device}')