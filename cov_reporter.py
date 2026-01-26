import os
import time
import shutil
import subprocess
import threading
import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional



def _to_bool(s: str, default: bool = False) -> bool:
    if s is None:
        return default
    return str(s).strip().lower() in ("1", "true", "yes", "y", "on")


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _resolve_under(base: Path, maybe_relative: str) -> Path:
    """
    Resolve a path relative to base. If the ini contains an absolute path,
    we reject it (you asked for relative-only).
    """
    p = Path(maybe_relative)
    if p.is_absolute():
        raise ValueError(f"Absolute paths are not allowed in config: {maybe_relative}")
    return (base / p).resolve()


@dataclass
class CoverageConfig:
    repo_root: Path
    build_dir: Path

    # AFL findings dir (resolved at runtime)
    findings_dir: Path

    reports_subdir: str
    run_id: str = ""

    interval_sec: int = 3600
    startup_delay_sec: int = 30

    lcov_ignore_errors: str = "mismatch,version,gcov,source,inconsistent"
    genhtml_ignore_errors: str = "empty,inconsistent,source"
    synthesize_missing: bool = True

    gcov_tool: Optional[Path] = None

    open_firefox: bool = False
    open_first_report: bool = True
    open_newest_each_time: bool = True
    artifacts_subdir: str = "gcov_artifacts"

    @property
    def artifacts_dir(self) -> Path:
        return (self.run_root / self.artifacts_subdir).resolve()

    @property
    def run_root(self) -> Path:
        if not self.run_id:
            raise RuntimeError("CoverageConfig.run_id not set")
        return (self.findings_dir / self.reports_subdir / self.run_id).resolve()

    @staticmethod
    def load_ini(ini_path: Path, findings_dir: Optional[Path]) -> "CoverageConfig":
        cp = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation())
        if not ini_path.exists():
            raise FileNotFoundError(f"coverage ini not found: {ini_path}")
        cp.read(ini_path)

        sec = cp["coverage"]

        # repo_root is relative to current working directory
        repo_root_raw = sec.get("repo_root", ".").strip()
        repo_root = _resolve_under(Path.cwd(), repo_root_raw)

        build_dir = _resolve_under(repo_root, sec.get("build_dir", "build/px4_sitl_default").strip())

        # findings dir comes from harness (preferred). If not provided, use fallback_findings_dir relative to repo_root.
        if findings_dir is None:
            fallback = sec.get("fallback_findings_dir", "findings").strip()
            findings_dir = _resolve_under(repo_root, fallback)
        else:
            if Path(findings_dir).is_absolute():
                # user asked: no full paths; require caller to pass relative too
                raise ValueError(f"Absolute findings_dir not allowed: {findings_dir}")
            findings_dir = _resolve_under(Path.cwd(), str(findings_dir))

        gcov_tool_raw = sec.get("gcov_tool", "").strip()
        gcov_tool = _resolve_under(repo_root, gcov_tool_raw) if gcov_tool_raw else None
        artifacts_subdir = sec.get("artifacts_subdir", "gcov_artifacts").strip()


        return CoverageConfig(
            repo_root=repo_root,
            build_dir=build_dir,
            findings_dir=findings_dir,
            reports_subdir=sec.get("reports_subdir", "coverage_reports").strip(),
            interval_sec=int(sec.get("interval_sec", "3600")),
            startup_delay_sec=int(sec.get("startup_delay_sec", "30")),
            lcov_ignore_errors=sec.get("lcov_ignore_errors", "mismatch,version,gcov,source,inconsistent").strip(),
            genhtml_ignore_errors=sec.get("genhtml_ignore_errors", "empty,inconsistent,source").strip(),
            synthesize_missing=_to_bool(sec.get("synthesize_missing", "true"), True),
            gcov_tool=gcov_tool,
            open_firefox=_to_bool(sec.get("open_firefox", "false"), False),
            open_first_report=_to_bool(sec.get("open_first_report", "true"), True),
            artifacts_subdir=artifacts_subdir,
            open_newest_each_time=_to_bool(sec.get("open_newest_each_time", "true"), True),
        )


class CoverageReporter:
    def __init__(self, cfg: CoverageConfig):
        self.cfg = cfg
        self._opened_first = False
        self._lock = threading.Lock()

    def _run_cmd(self, cmd: list[str]) -> None:
        print("[coverage] $ " + " ".join(cmd), flush=True)
        subprocess.run(cmd, check=True)

    def _newest_gcda_mtime(self) -> float:
        # find newest .gcda timestamp under build_dir
        newest = 0.0
        for p in self.cfg.build_dir.rglob("*.gcda"):
            try:
                newest = max(newest, p.stat().st_mtime)
            except FileNotFoundError:
                pass
        return newest

    def _write_proof_file(self, html_dir: Path, gcda_mtime: float) -> None:
        # writes a tiny file into the HTML dir so you can confirm which inputs were used
        proof = html_dir / "_proof.txt"
        proof.write_text(
            f"generated_at={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"build_dir={self.cfg.build_dir}\n"
            f"repo_root={self.cfg.repo_root}\n"
            f"newest_gcda_mtime={gcda_mtime}\n",
            encoding="utf-8"
        )
        
    def _flush_px4_gcov(self) -> None:
        """
        Ask PX4 to flush coverage counters to .gcda.
        This assumes your PX4 process has a SIGUSR1 handler that calls __gcov_flush()
        via LD_PRELOAD or an in-tree implementation.
        """
        # Best effort: don't fail if pkill finds nothing
        subprocess.run(
            ["bash", "-lc", "pkill -USR1 -f 'px4_sitl_default/bin/px4' || true"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.25)  # give filesystem a moment

    def _newest_gcda_mtime(self) -> float:
        """
        Return newest mtime among all .gcda under build_dir. 0.0 if none found.
        """
        newest = 0.0
        for p in self.cfg.build_dir.rglob("*.gcda"):
            try:
                mt = p.stat().st_mtime
                if mt > newest:
                    newest = mt
            except FileNotFoundError:
                continue
        return newest

    def _write_proof_file(self, html_dir: Path, gcda_mtime: float, info_path: Path) -> None:
        """
        Write a small proof file so you can confirm reports are changing.
        Stores:
          - newest gcda mtime
          - sha256 of lcov info
          - timestamp
        """
        try:
            # sha256 of the info file
            h = subprocess.check_output(
                ["bash", "-lc", f"sha256sum '{info_path}' | awk '{{print $1}}'"],
                text=True
            ).strip()

            proof = html_dir / "proof.txt"
            proof.write_text(
                "coverage proof\n"
                f"generated_ts    : {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"newest_gcda_mtime: {gcda_mtime:.3f}\n"
                f"lcov_info       : {info_path.name}\n"
                f"lcov_sha256     : {h}\n",
                encoding="utf-8"
            )
        except Exception as e:
            print(f"[coverage] warning: failed to write proof file: {e}", flush=True)

    def generate_once(self) -> Path:
        cfg = self.cfg
        cfg.run_root.mkdir(parents=True, exist_ok=True)
        cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)

        # --- 1) Force PX4 to flush gcov counters to .gcda ---
        # Requires you started PX4 with LD_PRELOAD=./scripts/libgcov_flush.so
        # (or another mechanism that makes SIGUSR1 call __gcov_flush()).
        self._flush_px4_gcov()

        # --- 2) Freshness gate: confirm .gcda is updating ---
        gcda_mtime = self._newest_gcda_mtime()
        now = time.time()

        if gcda_mtime == 0.0:
            raise RuntimeError(f"No .gcda files found under {cfg.build_dir}")

        # We expect at least *some* gcda touched very recently after flush.
        # Tune the window if your system is slow.
        if (now - gcda_mtime) > 10:
            raise RuntimeError(
                f"Newest .gcda is too old ({now - gcda_mtime:.1f}s). "
                f"Either PX4 isn't flushing coverage, or build_dir is wrong: {cfg.build_dir}"
            )

        # --- 3) Paths for this report ---
        ts = time.strftime("%Y%m%d_%H%M%S")
        info_path = cfg.run_root / f"lcov_{ts}.info"
        html_dir = cfg.run_root / f"html_{ts}"
        index_html = html_dir / "index.html"

        # --- 4) lcov / genhtml commands (REAL LISTS, NO "...") ---
        lcov_cmd = [
            "lcov",
            "--directory", str(cfg.build_dir),
            "--base-directory", str(cfg.repo_root),
            "--capture",
            "--ignore-errors", cfg.lcov_ignore_errors,
            "-o", str(info_path),
        ]
        if cfg.gcov_tool:
            lcov_cmd += ["--gcov-tool", str(cfg.gcov_tool)]

        genhtml_cmd = [
            "genhtml",
            "--ignore-errors", cfg.genhtml_ignore_errors,
        ]
        if cfg.synthesize_missing:
            genhtml_cmd += ["--synthesize-missing"]
        genhtml_cmd += [str(info_path), "-o", str(html_dir)]

        # --- 5) Run ---
        self._run_cmd(lcov_cmd)
        self._run_cmd(genhtml_cmd)

        if not index_html.exists():
            raise RuntimeError(f"Expected {index_html} to exist, but it doesn't.")

        # --- 6) Proof/debug breadcrumbs (optional but helpful) ---
        self._write_proof_file(html_dir, gcda_mtime, info_path)

        return index_html



    def maybe_open_firefox(self, index_html: Path) -> None:
        if not self.cfg.open_firefox:
            return

        firefox = _which("firefox")
        if not firefox:
            print("[coverage] firefox not found; skipping.", flush=True)
            return

        url = index_html.resolve().as_uri()

        with self._lock:
            if self.cfg.open_first_report and not self._opened_first:
                self._opened_first = True
                should_open = True
            else:
                should_open = self.cfg.open_newest_each_time

        if should_open:
            print(f"[coverage] opening: {url}", flush=True)
            subprocess.Popen([firefox, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class CoverageThread(threading.Thread):
    def __init__(self, reporter: CoverageReporter):
        super().__init__(daemon=True)
        self.reporter = reporter
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        cfg = self.reporter.cfg

        if cfg.startup_delay_sec > 0:
            time.sleep(cfg.startup_delay_sec)

        while not self._stop.is_set():
            try:
                idx = self.reporter.generate_once()
                self.reporter.maybe_open_firefox(idx)
            except Exception as e:
                print(f"[coverage] ERROR: {e}", flush=True)

            remaining = max(1, cfg.interval_sec)
            while remaining > 0 and not self._stop.is_set():
                time.sleep(1)
                remaining -= 1


def make_run_id(prefix: str = "cov") -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    pid = os.getpid()
    afl_id = os.getenv("AFL_FUZZER_ID", "").strip()
    afl_tag = f"afl_{afl_id}_" if afl_id else ""
    return f"{prefix}_{afl_tag}{ts}_pid{pid}"


def start_coverage_thread_from_ini(
    ini_path: str = "coverage.ini",
    findings_dir: Optional[str] = None,
    run_id: Optional[str] = None,
) -> CoverageThread:
    """
    findings_dir: pass AFL's -o dir as a RELATIVE path (e.g. "out/findings").
                 If None, uses fallback_findings_dir from the ini (relative to repo_root).
    """
    ini = Path(ini_path).resolve()

    findings_path: Optional[Path] = None
    if findings_dir:
        findings_path = Path(findings_dir)
        if findings_path.is_absolute():
            raise ValueError("findings_dir must be relative, not absolute")

    cfg = CoverageConfig.load_ini(ini, findings_dir=findings_path)
    cfg.run_id = run_id or make_run_id()

    reporter = CoverageReporter(cfg)
    t = CoverageThread(reporter)
    t.start()
    print(f"[coverage] repo_root     : {cfg.repo_root}", flush=True)
    print(f"[coverage] build_dir     : {cfg.build_dir}", flush=True)
    print(f"[coverage] findings_dir  : {cfg.findings_dir}", flush=True)
    print(f"[coverage] run_root      : {cfg.run_root}", flush=True)
    return t
