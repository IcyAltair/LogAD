docker compose up -d --build
curl -s localhost:8080/api/health                  # {"status":"ok",...}
curl -s 'localhost:8080/api/products?limit=2' | jq
wc -l data/nginx-logs/access.json.log              # grows by about 8 lines/s
docker compose exec app python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/metrics').read().decode()[:600])"