#!/bin/bash

rm -rf findings/default

AFL_FORKSRV_INIT_TMOUT=60000 AFL_DEBUG=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 py-afl-fuzz -t 2000 -i seeds -o findings -- python3 harnessPersistent.py @@
