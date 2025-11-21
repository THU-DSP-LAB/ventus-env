#!/bin/bash
# Script to apply fmt compatibility fixes for ventus-env
# This fixes compilation issues with fmt v11/v12 compatibility

set -e

echo "Applying fmt compatibility fixes..."

# Check if patch file exists
if [ ! -f "ventus-fmt-fix-full.patch" ]; then
    echo "Error: ventus-fmt-fix-full.patch not found!"
    echo "Please make sure the patch file is in the current directory."
    exit 1
fi

# Apply the patch
echo "Applying patch file..."
git apply ventus-fmt-fix-full.patch

echo "✅ Patch applied successfully!"
echo ""
echo "Summary of fixes:"
echo "  - Fixed SystemC type formatting in cyclesim"
echo "  - Added fmt/ranges.h includes where needed"
echo "  - Fixed sc_signal read() calls"
echo "  - Prioritized system libraries over conda/miniforge"
echo "  - Fixed Verilator PRINTF_COND duplicate definition warnings"
echo ""
echo "You can now run: bash build-ventus.sh"
