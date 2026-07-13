import py_compile
py_compile.compile("pem_probe.py", doraise=True)
py_compile.compile("pem_sentinel.py", doraise=True)
import io, sys, contextlib
import pem_probe as pp
print("import OK")

sys.argv = ["pem_probe.py", "--help"]
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf):
        pp.main()
except SystemExit:
    pass
help_txt = buf.getvalue()
for k in ("news_sentiment_cache", "hyperliquid_socket", "firecrawl", "github", "hyperliquid"):
    assert k in help_txt, f"missing choice {k}"
print("CLI choices OK")

res = pp.run_probes(["news_sentiment_cache"])
print("news probe ran:", res[0]["source"], res[0]["status"])
res2 = pp.run_probes(["hyperliquid_socket"])
print("hl socket probe ran:", res2[0]["source"], res2[0]["status"])
# unknown source guard
res3 = pp.run_probes(["bogus"])
print("unknown guard:", res3[0]["source"], res3[0]["ok"])
