import os
from datetime import timedelta

import torch
import torch.distributed as dist


def main():
    rank = int(os.environ["RANK"])
    local_rank = int(os.environ["LOCAL_RANK"])
    world_size = int(os.environ["WORLD_SIZE"])
    assert world_size == 16, world_size
    assert torch.cuda.device_count() == 8
    torch.cuda.set_device(local_rank)
    torch.manual_seed(rank)
    tensor = torch.rand(16, device="cuda")
    before = tensor[0].item()

    dist.init_process_group("nccl", timeout=timedelta(seconds=90))
    try:
        dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
        expected = torch.zeros_like(tensor)
        generator = torch.Generator(device=tensor.device)
        for seed in range(world_size):
            generator.manual_seed(seed)
            expected += torch.rand(16, device=tensor.device, generator=generator)
        torch.testing.assert_close(tensor, expected)
        print(
            f"ALL_REDUCE_OK rank={rank} before={before:.8f} "
            f"after={tensor[0].item():.8f} expected={expected[0].item():.8f}",
            flush=True,
        )
        dist.barrier()
    finally:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
