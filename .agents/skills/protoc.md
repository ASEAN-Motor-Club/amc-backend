## protoc (Protocol Buffers Compiler)

Always use the **official protoc binary** from GitHub releases. Do NOT use `bunx protoc` or npm wrappers.

### Download
- Releases: https://github.com/protocolbuffers/protobuf/releases
- Latest as of writing: v34.1
- Architecture-specific zip files (e.g., `protoc-34.1-linux-x86_64.zip`)

### Quick setup
```bash
mkdir -p /tmp/protoc
python3 -c "
import urllib.request
url = 'https://github.com/protocolbuffers/protobuf/releases/download/v34.1/protoc-34.1-linux-x86_64.zip'
urllib.request.urlretrieve(url, '/tmp/protoc/protoc.zip')
"
cd /tmp/protoc && unzip -q protoc.zip
./bin/protoc --version  # libprotoc 34.1
```

### Regenerate Python stubs
```bash
/tmp/protoc/bin/protoc \
  --python_out=src/amc/api \
  --proto_path=src/amc/api \
  src/amc/api/player_positions.proto
```

### Notes
- The project uses protobuf Python runtime 7.34.1 (from uv2nix amc-backend-env)
- Generated `*_pb2.py` files are checked into git
- Only `protoc --python_out` is needed (no grpc plugin used here)
