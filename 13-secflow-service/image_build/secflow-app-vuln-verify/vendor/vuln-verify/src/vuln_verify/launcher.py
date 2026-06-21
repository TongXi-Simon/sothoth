from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import subprocess
import tempfile
import time

from vuln_dispatch.log import get_logger
from vuln_verify.prompt import load_prompt

RATE_LIMIT_RETRY_DELAY_SECONDS = 30


def _is_rate_limited_output(text: str) -> bool:
    lowered = str(text or "").lower()
    return "429" in lowered or "rate limit" in lowered or "too many requests" in lowered


def _verify_one(
    group_dir: Path,
    out_dir: Path,
    base_prompt: str,
    prompt_msg: str,
    model: str | None,
    tmp_files: list[Path],
    session_dir: Path,
) -> tuple[str, int]:
    """对一个分组执行 pi 验证，返回 (group_id, exit_code)。"""
    log = get_logger("vuln_verify.launcher")

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=f"_{group_dir.name}_verifier_prompt.md",
        delete=False,
    ) as handle:
        handle.write(base_prompt)
        tmp_prompt = Path(handle.name)
    tmp_files.append(tmp_prompt)

    log.info("verifier_start", group_id=group_dir.name)

    stdout_file = out_dir / f"{group_dir.name}.stdout"
    stderr_file = out_dir / f"{group_dir.name}.stderr"

    pi_cmd = ["pi", "--session-dir", str(session_dir), "--append-system-prompt", str(tmp_prompt), "-p", prompt_msg]
    if model:
        pi_cmd.extend(["--model", model])

    consecutive_rate_limit_count = 0
    while True:
        with open(stdout_file, "w") as f_out, open(stderr_file, "w") as f_err:
            process = subprocess.Popen(
                pi_cmd,
                cwd=str(group_dir),
                stdout=f_out,
                stderr=f_err,
            )
            returncode = process.wait()
        if returncode == 0:
            break
        stderr_text = stderr_file.read_text(encoding="utf-8", errors="ignore") if stderr_file.exists() else ""
        stdout_text = stdout_file.read_text(encoding="utf-8", errors="ignore") if stdout_file.exists() else ""
        combined = f"{stderr_text}\n{stdout_text}"
        if not _is_rate_limited_output(combined):
            break
        consecutive_rate_limit_count += 1
        log.warning(
            "verifier_rate_limited",
            group_id=group_dir.name,
            consecutive_rate_limit_count=consecutive_rate_limit_count,
            retry_delay_seconds=RATE_LIMIT_RETRY_DELAY_SECONDS,
        )
        time.sleep(RATE_LIMIT_RETRY_DELAY_SECONDS)

    if returncode == 0:
        log.info("verifier_ok", group_id=group_dir.name)
    else:
        log.warning("verifier_fail", group_id=group_dir.name, exit_code=returncode)

    return (group_dir.name, returncode)


def launch(
    assembled_dir: Path,
    threat_path: str | None = None,
    model: str | None = None,
    concurrency: int = 4,
    resume: bool = False,
    session_dir: Path | None = None,
    source_root: Path | str | None = None,
    binary_root: Path | str | None = None,
) -> None:
    log = get_logger("vuln_verify.launcher")
    """
    遍历 {assembled_dir}/groups/group_*/ 目录。
    每个分组启动一个 pi 进程，最多 concurrency 个并发。
    将 verifier 输出写入 {assembled_dir}/verifier_output/，并记录里程碑结构化日志。
    当 resume=True 时，跳过已存在 verifier_output/group_XXX.done 标记的分组。
    """
    assembled_path = Path(assembled_dir)
    groups_dir = assembled_path / "groups"
    out_dir = (assembled_path / "verifier_output").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    session_path = (session_dir or assembled_path.parent / "run").resolve()
    session_path.mkdir(parents=True, exist_ok=True)

    if not groups_dir.is_dir():
        raise FileNotFoundError(f"groups directory not found: {groups_dir}")

    group_dirs = sorted(
        path for path in groups_dir.iterdir()
        if path.is_dir() and path.name.startswith("group_")
    )

    if resume:
        skipped = 0
        pending: list[Path] = []
        for g in group_dirs:
            if (out_dir / f"{g.name}.done").exists():
                log.info("verifier_skip", group_id=g.name, reason="already completed")
                skipped += 1
            else:
                pending.append(g)
        if skipped:
            log.info("verifier_resume", skipped_count=skipped, pending_count=len(pending))
        group_dirs = pending

    base_prompt = load_prompt(threat_path)
    tmp_files: list[Path] = []

    source_root_text = str(Path(source_root).resolve()) if source_root else ""
    binary_root_text = str(Path(binary_root).resolve()) if binary_root else ""
    binary_hint = (
        f"二进制根目录 binary_root 为：{binary_root_text}。"
        if binary_root_text
        else "优先使用源码完成验证。"
    )
    skill_prompt = """

## SecFlow Skills Lifecycle

本次验证运行在 SecFlow PI Agent 环境中，内置 skills 位于 `/root/.pi/agent/skills/`（或环境变量 `PI_CODING_AGENT_DIR` 指向目录下的 `skills/`）。

在开始验证前，必须先读取并执行以下知识检索 skills：
1. `wiki-mount`：检索与本次报告、项目、漏洞类型、false positive 模式相关的 wiki 知识。
2. `skill-recall-pull`：检索可复用的远程经验和技能。

验证完成并写出所有 `result_*.json` 后，必须按顺序执行 post-task lifecycle skills。某一步失败时记录失败原因并继续后续步骤，不得静默跳过：
1. `post-task-reflect`
2. `distill-experience`
3. `task-score`
4. `task-trace`
5. `vuln-report`
6. `skill-recall-onboard`
7. `sec-skill-local-evolve`
8. `skill-recall-propose`
9. `task-collect`

执行约束：
- 所有 skill 相关脚本都从 skills 目录读取，不要从网络下载 skill。
- 如存在 `~/.config/secocto/.env`，执行涉及 trace、recall、restore、report 的脚本前只加载环境变量，不得打印、写入或泄露任何环境变量值。
- `task-trace` 在 `SECOCTO_AGENT_TYPE=pi` 下使用当前 `cwd/run/*.jsonl` transcript；如无法定位 transcript，记录原因但不要影响 `result_*.json` 输出。
- `vuln-report` 只提交高置信 confirmed 漏洞；没有高置信漏洞时明确跳过。
"""
    prompt_msg = (
        "分析 reports/ 下的漏洞报告。"
        f"源码根目录 source_root 为：{source_root_text}。"
        f"{binary_hint}"
        "请从报告正文中提取文件路径、函数名、行号等定位信息，并在 source_root 下查找对应源码文件。"
        "如果报告路径是 openGauss-server-master/xxx.cpp，则源码文件通常位于 "
        f"{source_root_text}/openGauss-server-master/xxx.cpp。"
        "只在 source_root 指向的源码根目录内查找源码文件，不要访问项目外路径。"
        f"将 result_*.json 输出到 {out_dir}。"
        "不需要生成任何 .md 文件，仅输出 JSON 格式的验证结果。"
        f"{skill_prompt}"
    )

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    _verify_one, g, out_dir, base_prompt, prompt_msg, model, tmp_files, session_path
                ): g
                for g in group_dirs
            }

            ok_count = 0
            error_count = 0
            for future in as_completed(futures):
                group_id, returncode = future.result()
                if returncode == 0:
                    ok_count += 1
                    (out_dir / f"{group_id}.done").touch()
                else:
                    error_count += 1

        log.info("verifier_summary",
                 total_group_count=len(group_dirs),
                 ok_count=ok_count,
                 error_count=error_count)

        if error_count:
            raise RuntimeError(f"verifier failed: {error_count}/{len(group_dirs)} groups exited with error")
    finally:
        for tmp_file in tmp_files:
            try:
                tmp_file.unlink()
            except FileNotFoundError:
                pass
