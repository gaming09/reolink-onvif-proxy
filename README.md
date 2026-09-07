# Reolink ONVIF Proxy

Reolink ONVIF Proxy repairs malformed HTTP responses produced by some Reolink firmware so standards-compliant ONVIF clients such as Frigate can use the camera's native PTZ service.

The proxy forwards ONVIF requests to one local camera and adds a valid HTTP response envelope only when the camera replies with a raw XML/SOAP document. Valid HTTP responses pass through unchanged. Video and audio continue to flow directly between the camera and Frigate.

```text
Frigate PTZ controls -> Reolink ONVIF Proxy :18890 -> Reolink camera :8000
```

The container has no cloud integration, telemetry, camera credentials, share mounts, or Docker socket access. Frigate remains responsible for ONVIF authentication and sends the existing camera credentials through the proxy to the camera.

## Unraid installation

The included DockerMan template provides editable fields for the proxy port, camera name, camera address, native ONVIF port, timeout, optional client allowlist, and log level. Install one container per affected camera and assign each a unique host port. A camera whose ONVIF service already works does not need a proxy instance.

For an affected camera, choose an unused host port such as `18890`, enter the camera's LAN address, and normally leave the native ONVIF port at `8000`. Then configure Frigate's existing camera:

```yaml
onvif:
  host: <unraid-server-address>
  port: 18890
  user: <camera-username>
  password: <existing camera password>
  tls_insecure: true
  ignore_time_mismatch: true
```

Open `http://UNRAID_SERVER_IP:18890/health` to verify the container. The response reports the configured camera name and target without exposing credentials.

## Local Docker run

```bash
docker run -d \
  --name reolink-onvif-proxy \
  -p 18890:18890/tcp \
  -e CAMERA_NAME=reolink-camera \
  -e CAMERA_HOST=192.0.2.25 \
  -e CAMERA_PORT=8000 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cap-drop=ALL \
  --security-opt=no-new-privileges:true \
  ghcr.io/gaming09/reolink-onvif-proxy:latest
```

| Variable | Default | Purpose |
|---|---:|---|
| `CAMERA_NAME` | `reolink-camera` | Friendly label used in health output and logs |
| `CAMERA_HOST` | required | Camera LAN IP address or hostname |
| `CAMERA_PORT` | `8000` | Camera's native ONVIF service port |
| `LISTEN_PORT` | `18890` | Internal proxy listener port |
| `IO_TIMEOUT` | `15` | Socket timeout in seconds |
| `ALLOWED_CLIENTS` | empty | Optional comma-separated client IP/CIDR allowlist |
| `LOG_LEVEL` | `INFO` | Python log level |

MIT licensed. This project is not affiliated with or endorsed by Reolink, Frigate, or Unraid.
