"""Inference benchmark aggregation without live hardware or an API server."""

from tools.benchmark_inference import compare, summarize


def test_summary_and_cpu_gpu_comparison() -> None:
    gpu = summarize(
        "auto",
        [
            {
                "actual_device": "cuda",
                "elapsed_s": 0.0,
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
                "elapsed_s": 2.0,
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
                "elapsed_s": 0.0,
                "inferences": 2,
                "inference_ms": 84.0,
                "cpu_percent": 90.0,
                "cpu_power_w": 30.0,
                "memory_mb": 900.0,
                "gpu_power_w": 6.0,
            },
            {
                "actual_device": "cpu",
                "elapsed_s": 2.0,
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
    assert gpu["observed_inference_fps"] == 6.0
    assert gpu["inference_ms_mean"] == 21.0
    assert result["gpu_inference_speedup_x"] == 4.0
    assert result["gpu_observed_throughput_x"] == 4.0
    assert result["gpu_incremental_board_power_w"] == 15.0


def test_summary_reports_intervals_with_no_new_inference() -> None:
    summary = summarize(
        "cpu",
        [
            {"elapsed_s": 0.0, "inferences": 10, "inference_ms": 100.0},
            {"elapsed_s": 2.0, "inferences": 10, "inference_ms": 100.0},
            {"elapsed_s": 4.0, "inferences": 12, "inference_ms": 120.0},
        ],
    )

    assert summary["observed_inference_fps"] == 0.5
    assert summary["no_progress_s"] == 2.0
    assert summary["no_progress_intervals"] == 1
    assert summary["inference_ms_mean"] == 110.0


def test_pipeline_throughput_does_not_count_extra_motion_rescan_calls() -> None:
    summary = summarize(
        "auto",
        [
            {"elapsed_s": 0.0, "inferences": 100, "pipeline_ticks": 50},
            {"elapsed_s": 2.0, "inferences": 115, "pipeline_ticks": 62},
        ],
    )

    assert summary["inferences_observed"] == 15
    assert summary["model_calls_per_s"] == 7.5
    assert summary["pipeline_results_observed"] == 12
    assert summary["observed_inference_fps"] == 6.0


def test_detector_off_is_a_baseline_not_a_pipeline_stall() -> None:
    summary = summarize(
        "off",
        [
            {
                "actual_device": "off",
                "elapsed_s": 0.0,
                "inferences": 100,
                "pipeline_ticks": 50,
                "cpu_percent": 42.0,
            },
            {
                "actual_device": "off",
                "elapsed_s": 2.0,
                "inferences": 100,
                "pipeline_ticks": 50,
                "cpu_percent": 40.0,
            },
        ],
    )

    assert summary["actual_device"] == "off"
    assert summary["pipeline_results_observed"] == 0
    assert summary["observed_inference_fps"] == 0.0
    assert summary["no_progress_s"] == 0.0
    assert summary["no_progress_intervals"] == 0
    assert summary["cpu_percent_mean"] == 41.0
