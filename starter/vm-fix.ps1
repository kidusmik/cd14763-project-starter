@'
import bedrock_agentcore_starter_toolkit, pathlib

pkg = pathlib.Path(bedrock_agentcore_starter_toolkit.__file__).parent
for file in pkg.rglob("*.py"):
    txt = file.read_text(encoding="utf-8", errors="ignore")
    if "LINUX_CONTAINER" in txt or "amazonlinux2-x86_64-standard:5.0" in txt:
        txt = txt.replace("LINUX_CONTAINER", "ARM_CONTAINER")
        txt = txt.replace("amazonlinux2-x86_64-standard:5.0", "amazonlinux2-aarch64-standard:3.0")
        file.write_text(txt, encoding="utf-8")
        print(f"Restored ARM64 in: {file.name}")
'@ | Set-Content -Path .\restore_arm.py -Encoding UTF8

uv run python restore_arm.py