#!/usr/bin/env python3
# Background: reproduce the pinned texture software path from one env checkout.
# Flow: inspect pinned sources, build local components, run numerical/image gates.
# Usage: python3 tools/texture_repro.py {check,build,verify} --help
# Maintenance: implementation and frozen acceptance matrix live in texture_repro/.
from texture_repro.workflow import main

if __name__ == '__main__':
    raise SystemExit(main())
