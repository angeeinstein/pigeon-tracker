"""Inference benchmark aggregation without live hardware or an API server."""

from tools.benchmark_inference import compare, summarize


def test_summary_and_cpu_gpu_comparison() -> None:
    gpu = summarize(
        "auto",
        [
            {
                "actual_device": "cuda",
                "inferences": 10,
                "inference_ms": 20.0,
                "effective_fps": 6.0,
                "cpu_percent": 25.0,
                "cpu_cores": 0.5,
                "cpu_power_w": 15.0,
                "memory_mb": 800.0,
                "gpu_power_w": 20.0,
                "gpu_util_percent": 8.0,
                "gpu_memory_mb": 160.0,
                "gpu_temperature_c": 65.0,
            },
            {
                "actual_device": "cuda",
                "inferences": 22,
                "inference_ms": 22.0,
                "effective_fps": 6.0,
                "cpu_percent": 27.0,
                "cpu_cores": 0.54,
                "cpu_power_w": 16.0,
                "memory_mb": 820.0,
                "gpu_power_w": 22.0,
                "gpu_util_percent": 10.0,
                "gpu_memory_mb": 162.0,
                "gpu_temperature_c": 66.0,
            },
        ],
    )
    cpu = summarize(
        "cpu",
        [
            {
                "actual_device": "cpu",
                "inferences": 2,
                "inference_ms": 84.0,
                "cpu_percent": 90.0,
                "cpu_power_w": 30.0,
                "memory_mb": 900.0,
                "gpu_power_w": 6.0,
            },
            {
                "actual_device": "cpu",
                "inferences": 5,
                "inference_ms": 84.0,
                "cpu_percent": 92.0,
                "cpu_power_w": 31.0,
                "memory_mb": 920.0,
                "gpu_power_w": 6.0,
            },
        ],
    )

    result = compare([gpu, cpu])

    assert gpu["inferences_observed"] == 12
    assert gpu["inference_ms_mean"] == 21.0
    assert result["gpu_inference_speedup_x"] == 4.0
    assert result["gpu_incremental_board_power_w"] == 15.0
