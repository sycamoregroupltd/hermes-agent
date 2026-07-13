import sys, importlib
print("python", sys.version.split()[0])
for m in ["duckdb", "polars", "numpy"]:
    try:
        mod = importlib.import_module(m)
        print(m, getattr(mod, "__version__", "?"))
    except Exception as e:
        print(m, "IMPORT FAIL", repr(e))

# Test DB connectivity via docker exec over TCP (bypass Kong)
import subprocess
q = "SELECT count(*) AS n FROM signal_journeys;"
cmd = [
    "docker", "exec", "-e", "PGPASSWORD=postgres",
    "sycodetrading-supabase-db", "psql", "-h", "localhost", "-U", "postgres",
    "-d", "postgres", "-tAc", q,
]
try:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    print("DB signal_journeys count:", out.stdout.strip(), "err:", out.stderr.strip()[:200])
except Exception as e:
    print("DB FAIL", repr(e))
