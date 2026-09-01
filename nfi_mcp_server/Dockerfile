FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY __init__.py bots.py freqtrade_client.py green_streak.py json_utils.py pair_param_calibration.py \
     pair_priority.py pair_quality.py server.py signal_control.py stats.py \
     ./nfi_mcp_server/

CMD ["python", "-m", "nfi_mcp_server.server"]
