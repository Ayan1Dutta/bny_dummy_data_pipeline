from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from deploy import bluegreen

ROOT = Path(__file__).resolve().parents[1]
NGINX_DIR = ROOT / "deploy" / "nginx"


def test_shipped_active_conf_starts_on_blue_and_matches_the_controller_template():
    assert (NGINX_DIR / "active.conf").read_text(encoding="utf-8") == bluegreen.render_active_conf("blue")


def test_switching_to_green_changes_both_the_upstream_and_the_served_by_label():
    conf = bluegreen.render_active_conf("green")
    assert "server api-green:8000;" in conf
    assert 'default "green";' in conf
    assert "api-blue" not in conf


def test_unknown_colour_is_rejected():
    with pytest.raises(ValueError):
        bluegreen.render_active_conf("red")


def test_nginx_site_proxies_everything_to_the_active_upstream():
    site = (NGINX_DIR / "default.conf").read_text(encoding="utf-8")
    assert "include /etc/nginx/bluegreen/active.conf;" in site
    assert "proxy_pass http://active_backend;" in site
    assert "add_header X-Served-By $active_color always;" in site
    assert "location = /lb/status" in site


def test_compose_runs_two_api_containers_and_one_nginx():
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load((ROOT / "docker-compose.bluegreen.yml").read_text(encoding="utf-8"))
    services = spec["services"]
    assert set(services) == {"api-blue", "api-green", "lb"}
    host_ports = {name: svc["ports"][0].rsplit(":", 2)[-2] for name, svc in services.items()}
    assert host_ports == {"api-blue": "8001", "api-green": "8002", "lb": "8000"}
    for name in ("api-blue", "api-green"):
        assert services[name]["pull_policy"] == "never"
        assert services[name]["env_file"] == f"deploy/env/{name.split('-')[1]}.env"


def test_warehouse_path_comes_from_the_colour_config_file_not_the_shared_block():
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load((ROOT / "docker-compose.bluegreen.yml").read_text(encoding="utf-8"))
    for name in ("api-blue", "api-green"):
        assert "WAREHOUSE_PATH" not in spec["services"][name].get("environment", {})


@pytest.mark.skipif(shutil.which("nginx") is None, reason="nginx binary not installed")
@pytest.mark.parametrize("color", ["blue", "green"])
def test_nginx_accepts_the_configuration(tmp_path, color):
    (tmp_path / "bluegreen").mkdir()
    active = bluegreen.render_active_conf(color).replace(f"api-{color}:8000", "127.0.0.1:8001")
    (tmp_path / "bluegreen" / "active.conf").write_text(active, encoding="utf-8")
    site = (NGINX_DIR / "default.conf").read_text(encoding="utf-8")
    site = site.replace("/etc/nginx/bluegreen/active.conf", str(tmp_path / "bluegreen" / "active.conf"))
    site = site.replace("listen 80;", "listen 127.0.0.1:18080;")
    (tmp_path / "site.conf").write_text(site, encoding="utf-8")
    (tmp_path / "nginx.conf").write_text(
        f"pid {tmp_path}/nginx.pid;\nerror_log {tmp_path}/error.log;\nevents {{}}\n"
        f"http {{\n  access_log off;\n  include {tmp_path}/site.conf;\n}}\n",
        encoding="utf-8",
    )
    proc = subprocess.run(["nginx", "-t", "-p", str(tmp_path), "-c", str(tmp_path / "nginx.conf")],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
