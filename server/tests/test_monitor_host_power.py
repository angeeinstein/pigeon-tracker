from pathlib import Path

from tools.monitor_host_power import RaplPower, _selected_gpus


def test_rapl_discovers_only_top_level_package_domains(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "name").write_text("package-0", encoding="utf-8")
    (package / "energy_uj").write_text("1000", encoding="utf-8")
    (package / "max_energy_range_uj").write_text("100000", encoding="utf-8")

    child = tmp_path / "child"
    child.mkdir()
    (child / "name").write_text("core", encoding="utf-8")
    (child / "energy_uj").write_text("500", encoding="utf-8")

    assert [domain.name for domain in RaplPower(tmp_path, [package, child]).domains] == [
        "package-0"
    ]


def test_selected_gpus_filters_host_indexes() -> None:
    gpus = [{"index": 0}, {"index": 1}]

    assert _selected_gpus(gpus, None) == gpus
    assert _selected_gpus(gpus, {1}) == [{"index": 1}]
