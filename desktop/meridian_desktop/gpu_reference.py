"""Vendor datasheet figures for the GPU models RunPod lists. Pure Python: no Qt import.

These are the peak figures the vendors print, not speed measured for your job. None means the vendor's datasheet does not
state that figure, so the table leaves it blank and the sort puts it last. Each entry names the page its figures came
from. They were looked up on 2026-11-15. Datasheets change, so check a figure before relying on it.

Two cautions. Consumer GeForce figures are the FP16-with-FP16-accumulate rows from NVIDIA's whitepapers; the same card's
FP32-accumulate row is half of that, so comparisons across card types are rough. Some dense figures are half of the
printed sparse figure, which NVIDIA's footnote says is the rule for that product, and the note says so.
"""

from __future__ import annotations

from dataclasses import dataclass

NVIDIA_A100 = "https://www.nvidia.com/en-us/data-center/a100/"
NVIDIA_A100_DATASHEET = "https://www.nvidia.com/content/dam/en-zz/Solutions/Data-Center/a100/pdf/nvidia-a100-datasheet-us-nvidia-1758950-r4-web.pdf"
NVIDIA_A40_DATASHEET = "https://images.nvidia.com/content/Solutions/data-center/a40/nvidia-a40-datasheet.pdf"
NVIDIA_DGX_B200 = "https://www.nvidia.com/en-us/data-center/dgx-b200/"
NVIDIA_HGX = "https://www.nvidia.com/en-us/data-center/hgx/"
H100_DATASHEET = "https://www.MEGWARE.com/fileadmin/user_upload/LandingPage%20NVIDIA/nvidia-h100-datasheet.pdf"
NVIDIA_H100 = "https://www.nvidia.com/en-us/data-center/h100/"
NVIDIA_H200 = "https://www.nvidia.com/en-us/data-center/h200/"
NVIDIA_L4 = "https://www.nvidia.com/en-us/data-center/l4/"
L40_DATASHEET = "https://www.pny.com/File%20Library/Company/Support/Product%20Brochures/NVIDIA%20Data%20Center%20GPUs/pny-nvidia-l40-datasheet.pdf"
NVIDIA_L40S = "https://www.nvidia.com/en-us/data-center/l40s/"
V100_DATASHEET = "https://images.nvidia.com/content/technologies/volta/pdf/tesla-volta-v100-datasheet.pdf"
HP_RTX_2000_ADA = "https://valuehub.hp.com/static/nvidia/docs/NVIDIA%20RTX%202000%20Ada%20Generation%20Datasheet.pdf"
NVIDIA_RTX_4000_ADA = "https://www.nvidia.com/content/dam/en-zz/Solutions/products/workstations/nvidia-rtx-4000-datasheet.pdf"
HP_RTX_5000_ADA = "https://valuehub.hp.com/static/nvidia/docs/NVIDIA%20RTX%205000%20Ada%20Generation%20Datasheet.pdf"
HP_RTX_6000_ADA = "https://valuehub.hp.com/static/nvidia/docs/NVIDIA%20RTX%206000%20Ada%20Generation%20Datasheet.pdf"
RTX_A2000_DATASHEET = "https://www.nvidia.com/content/dam/en-zz/Solutions/products/workstations/nvidia-rtx-a2000-12gb-datasheet.pdf"
RTX_A4000_DATASHEET = "https://www.nvidia.com/content/dam/en-zz/Solutions/products/workstations/nvidia-rtx-a4000-datasheet.pdf"
RTX_A4500_DATASHEET = "https://www.nvidia.com/content/dam/en-zz/Solutions/products/workstations/nvidia-rtx-a4500-datasheet.pdf"
RTX_A5000_DATASHEET = "https://www.nvidia.com/content/dam/en-zz/Solutions/products/workstations/nvidia-rtx-a5000-datasheet.pdf"
RTX_A6000_DATASHEET = "https://www.nvidia.com/content/dam/en-zz/Solutions/products/workstations/nvidia-rtx-a6000-datasheet.pdf"
RTX_PRO_4000 = "https://www.nvidia.com/en-us/products/workstations/professional-desktop-gpus/rtx-pro-4000/"
RTX_PRO_4500_DATASHEET = "https://www.nvidia.com/content/dam/en-zz/Solutions/data-center/rtx-pro-4500-blackwell/workstation-datasheet-blackwell-rtx-pro-4500-we-nvidia-us-4978450-web-new.pdf"
RTX_PRO_5000 = "https://www.nvidia.com/en-us/products/workstations/professional-desktop-gpus/rtx-pro-5000/"
RTX_PRO_6000_WORKSTATION_DATASHEET = "https://www.nvidia.com/content/dam/en-zz/Solutions/data-center/rtx-pro-6000-blackwell-workstation-edition/workstation-blackwell-rtx-pro-6000-workstation-edition-nvidia-us-3519208-web.pdf"
RTX_PRO_6000_SERVER = "https://www.nvidia.com/en-us/data-center/rtx-pro-6000-blackwell-server-edition/"
GA102_WHITEPAPER = "https://images.nvidia.com/aem-dam/en-zz/Solutions/geforce/ampere/pdf/NVIDIA-ampere-GA102-GPU-Architecture-Whitepaper-V1.pdf"
ADA_WHITEPAPER = "https://images.nvidia.com/aem-dam/Solutions/geforce/ada/nvidia-ada-gpu-architecture.pdf"
BLACKWELL_WHITEPAPER = "https://images.nvidia.com/aem-dam/Solutions/geforce/blackwell/nvidia-rtx-blackwell-gpu-architecture.pdf"
RTX_40_FAMILY = "https://www.nvidia.com/en-us/geforce/graphics-cards/40-series/rtx-4080-family/"
ROCM_MI300 = "https://rocm.docs.amd.com/en/latest/reference/gpu-arch/mi300.html"
ROCM_MI350 = "https://rocm.docs.amd.com/en/latest/reference/gpu-arch/mi350.html"


@dataclass(frozen=True)
class Spec:
    """One model's vendor figures. A None figure is one the source does not state."""

    fp16_tensor_tflops_dense: float | None
    memory_bandwidth_gbs: float | None
    source_url: str
    note: str


SPECS: dict[str, Spec] = {
    "NVIDIA A100 80GB PCIe": Spec(312, 1935, NVIDIA_A100, "312 dense; 624 with sparsity."),
    "NVIDIA A100-SXM4-40GB": Spec(312, 1555, NVIDIA_A100_DATASHEET, "40 GB SXM column of the June 2021 datasheet."),
    "NVIDIA A100-SXM4-80GB": Spec(312, 2039, NVIDIA_A100, "SXM column of the A100 page."),
    "NVIDIA A40": Spec(149.7, 696, NVIDIA_A40_DATASHEET, "149.7 dense; 299.4 with structural sparsity."),
    "NVIDIA B200": Spec(None, None, NVIDIA_DGX_B200, "The page gives system totals only, not per GPU."),
    "NVIDIA B300 SXM6 AC": Spec(None, None, NVIDIA_HGX, "The page gives an 8-GPU system total only."),
    "NVIDIA H100 80GB HBM3": Spec(989.5, 3350, H100_DATASHEET, "Half of the 1,979 sparse figure, per the footnote's rule."),
    "NVIDIA H100 NVL": Spec(None, 3900, NVIDIA_H100, "Only the sparse FP16 figure is printed."),
    "NVIDIA H100 PCIe": Spec(756.5, 2000, H100_DATASHEET, "Half of the 1,513 sparse figure, per the footnote's rule."),
    "NVIDIA H200": Spec(None, 4800, NVIDIA_H200, "Only the sparse FP16 figure is printed."),
    "NVIDIA H200 NVL": Spec(None, 4800, NVIDIA_H200, "Only the sparse FP16 figure is printed."),
    "NVIDIA L4": Spec(121, 300, NVIDIA_L4, "Half of the 242 sparse figure, per the footnote's rule."),
    "NVIDIA L40": Spec(181.05, 864, L40_DATASHEET, "181.05 dense; 362.1 with sparsity. Preliminary datasheet copy."),
    "NVIDIA L40S": Spec(362.05, 864, NVIDIA_L40S, "362.05 dense; 733 with sparsity."),
    "Tesla V100-PCIE-16GB": Spec(112, 900, V100_DATASHEET, "The table row is labelled Tensor Performance, not FP16."),
    "Tesla V100-SXM2-16GB": Spec(125, 900, V100_DATASHEET, "The table row is labelled Tensor Performance, not FP16."),
    "NVIDIA RTX 2000 Ada Generation": Spec(None, 224, HP_RTX_2000_ADA, "Only FP8 with sparsity is printed."),
    "NVIDIA RTX 4000 Ada Generation": Spec(None, 360, NVIDIA_RTX_4000_ADA, "Only FP8 with sparsity is printed."),
    "NVIDIA RTX 5000 Ada Generation": Spec(None, 576, HP_RTX_5000_ADA, "Only FP8 with sparsity is printed."),
    "NVIDIA RTX 6000 Ada Generation": Spec(None, 960, HP_RTX_6000_ADA, "Only FP8 with sparsity is printed."),
    "NVIDIA RTX A2000": Spec(None, 288, RTX_A2000_DATASHEET, "Only effective TFLOPS with sparsity is printed."),
    "NVIDIA RTX A4000": Spec(None, 448, RTX_A4000_DATASHEET, "Only effective TFLOPS with sparsity is printed."),
    "NVIDIA RTX A4500": Spec(None, 640, RTX_A4500_DATASHEET, "Only effective TFLOPS with sparsity is printed."),
    "NVIDIA RTX A5000": Spec(None, 768, RTX_A5000_DATASHEET, "Only effective TFLOPS with sparsity is printed."),
    "NVIDIA RTX A6000": Spec(None, 768, RTX_A6000_DATASHEET, "Only effective TFLOPS with sparsity is printed."),
    "NVIDIA RTX PRO 4000 Blackwell": Spec(None, 672, RTX_PRO_4000, "Only FP4 TOPS is printed."),
    "NVIDIA RTX PRO 4500 Blackwell": Spec(None, 896, RTX_PRO_4500_DATASHEET, "Only FP4 TOPS is printed. A highlight gives 784 GB/s."),
    "NVIDIA RTX PRO 5000 Blackwell": Spec(None, 1344, RTX_PRO_5000, "Only AI TOPS is printed, with no precision label."),
    "NVIDIA RTX PRO 6000 Blackwell Workstation Edition": Spec(None, 1792, RTX_PRO_6000_WORKSTATION_DATASHEET, "Only FP4 TOPS is printed."),
    "NVIDIA RTX PRO 6000 Blackwell Server Edition": Spec(None, 1597, RTX_PRO_6000_SERVER, "1 PFLOP FP16 is printed with no dense or sparse label; left blank."),
    "NVIDIA GeForce RTX 3070": Spec(81.3, 448, GA102_WHITEPAPER, "FP16-accumulate row, Founders Edition."),
    "NVIDIA GeForce RTX 3080": Spec(119.1, 760, BLACKWELL_WHITEPAPER, "FP16-accumulate row, 10 GB Founders Edition."),
    "NVIDIA GeForce RTX 3080 Ti": Spec(136.4, 912, ADA_WHITEPAPER, "FP16-accumulate row."),
    "NVIDIA GeForce RTX 3090": Spec(142.3, 936, BLACKWELL_WHITEPAPER, "FP16-accumulate row, Founders Edition."),
    "NVIDIA GeForce RTX 3090 Ti": Spec(160, 1008, ADA_WHITEPAPER, "FP16-accumulate row."),
    "NVIDIA GeForce RTX 4070 Ti": Spec(160.4, 504, BLACKWELL_WHITEPAPER, "FP16-accumulate row."),
    "NVIDIA GeForce RTX 4080": Spec(194.9, 716.8, ADA_WHITEPAPER, "16 GB variant, FP16-accumulate row. Not the SUPER."),
    "NVIDIA GeForce RTX 4080 SUPER": Spec(None, None, RTX_40_FAMILY, "Neither figure is printed on the family page."),
    "NVIDIA GeForce RTX 4090": Spec(330.3, 1008, ADA_WHITEPAPER, "FP16-accumulate row; the FP32-accumulate row is 165.2."),
    "NVIDIA GeForce RTX 5080": Spec(225.1, 960, BLACKWELL_WHITEPAPER, "FP16-accumulate row."),
    "NVIDIA GeForce RTX 5090": Spec(419, 1792, BLACKWELL_WHITEPAPER, "FP16-accumulate row."),
    "AMD Instinct MI300X OAM": Spec(1307.4, 5300, ROCM_MI300, "Dense reading inferred: the page does not label dense or sparse."),
    "AMD Instinct MI350 OAM": Spec(2300, 8000, ROCM_MI350, "Matched to MI350X by name. MI355X is 2,500 dense on the same page."),
}


def spec_for(gpu_id: str) -> Spec | None:
    """The vendor figures for one RunPod GPU id, or None when the reference has no entry for it."""
    return SPECS.get(gpu_id)


__all__ = ["SPECS", "Spec", "spec_for"]
