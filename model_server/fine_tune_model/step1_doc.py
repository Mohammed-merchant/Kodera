
import torch

if torch.cuda.is_available():
    device = torch.device("cuda")
else:
    device = torch.device("cpu")

print(f"Using device: {device}")
print(f"Torch version: {torch.__version__}")

if torch.cuda.is_available():
    print(f"GPU Name: {torch.cuda.get_device_name(0)}")

    for i in range(torch.cuda.device_count()):
        print(
            f"GPU {i} Properties: "
            f"{torch.cuda.get_device_properties(i)}"
        )
else:
    print("CUDA is not available. Running on CPU.")
