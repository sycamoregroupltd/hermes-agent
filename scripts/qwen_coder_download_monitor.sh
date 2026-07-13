#!/bin/bash
# Monitor Qwen3-Coder download completion
# Expected: 17665334432 bytes (17.7 GB)
# SHA256: 2841aa314d916434860cfb8990347528dcdfe5c350dbcb9d1461dbee88ff2533

TARGET="/home/frank/models/Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL.gguf"
EXPECTED_SIZE=17665334432
EXPECTED_SHA256="2841aa314d916434860cfb8990347528dcdfe5c350dbcb9d1461dbee88ff2533"

if [ ! -f "$TARGET" ]; then
    echo "NO_FILE"
    exit 0
fi

# Get size via stat
CURRENT_SIZE=$(stat -c%s "$TARGET" 2>/dev/null)
echo "SIZE=$CURRENT_SIZE ($(echo "scale=1; $CURRENT_SIZE / 1000000000" | bc)GB)"

# Check if complete
if [ "$CURRENT_SIZE" -eq "$EXPECTED_SIZE" ]; then
    echo "SIZE_MATCH=YES"
    echo "VERIFYING SHA256..."
    COMPUTED_SHA=$(sha256sum "$TARGET" | cut -d' ' -f1)
    if [ "$COMPUTED_SHA" = "$EXPECTED_SHA256" ]; then
        echo "SHA256_MATCH=YES"
        echo "DOWNLOAD_COMPLETE=1"
    else
        echo "SHA256: computed=$COMPUTED_SHA expected=$EXPECTED_SHA256"
        echo "SHA256_MATCH=NO"
        echo "DOWNLOAD_COMPLETE=0"
    fi
else
    PCT=$(echo "scale=1; $CURRENT_SIZE * 100 / $EXPECTED_SIZE" | bc)
    REMAINING=$(( ($EXPECTED_SIZE - $CURRENT_SIZE) / 1000000 ))
    echo "SIZE_MATCH=NO (${PCT}% complete, ~${REMAINING}MB remaining)"
    echo "DOWNLOAD_COMPLETE=0"
fi
