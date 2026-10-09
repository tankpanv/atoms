FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
COPY --from=node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && apt-get update && apt-get install -y --no-install-recommends git ripgrep curl ca-certificates libreoffice-impress libreoffice-writer libreoffice-calc fonts-noto-cjk fonts-crosextra-carlito fonts-crosextra-caladea \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN python -m playwright install --with-deps --only-shell chromium \
    && chmod -R a+rX /opt/playwright
# Lock-resolved default engineering environment. Copied independently into each project.
COPY templates/web-v1 /opt/atoms-template-cache/web-v1
RUN cd /opt/atoms-template-cache/web-v1 && npm ci --ignore-scripts --no-audit --no-fund && npm run build \
    && python -c "import hashlib,pathlib; p=pathlib.Path('.'); (p/'fingerprint').write_text(hashlib.sha256(b''.join((p/n).read_bytes() for n in ('package.json','package-lock.json','frontend/package.json'))).hexdigest())"
# Preinstall the default backend contract beside the frontend template. New
# fullstack projects copy this exact environment locally instead of downloading
# FastAPI/PostgreSQL dependencies again for every workspace.
COPY templates/api-v1/backend/requirements.txt /opt/atoms-template-cache/python-v1/requirements.txt
RUN mkdir -p /opt/atoms-template-cache/python-v1/.python-packages \
    && pip install --no-cache-dir --target /opt/atoms-template-cache/python-v1/.python-packages -r /opt/atoms-template-cache/python-v1/requirements.txt \
    && sha256sum /opt/atoms-template-cache/python-v1/requirements.txt | cut -d ' ' -f1 > /opt/atoms-template-cache/python-v1/fingerprint
COPY . .
# Image construction is an environment smoke check.  Project acceptance is
# performed by the selected harness tier against the current source/runtime;
# running the entire historical unit-test suite here made every worker image
# rebuild slow and allowed stale assertions to block deployment.
RUN PYTHONPATH=/app python -m compileall -q agent.py agent_service.py agent_harness.py build_tiers.py build_tools.py
RUN chmod -R a+rX /app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
