#!/bin/bash
# Launch the ROI review/editor Streamlit app on the VM (127.0.0.1:8501, SSH-tunnel only).
set -a
. /home/ubuntu/clinicalomxdb/hub-pipelines/prefect/workers/vm-process/.env
set +a
export PREFECT_API_URL=http://localhost:4200/api
cd /home/ubuntu/clinicalomx_pipeline
/home/ubuntu/clinicalomxdb/hub-pipelines/prefect/workers/vm-process/.venv/bin/python -m streamlit run \
  src/qptiff_pipeline/review/app.py \
  --server.port 8502 --server.address 127.0.0.1 --server.headless true
