# VPS Preparation Guide

Before deploying the Quant India platform to a VPS (e.g. AWS EC2, DigitalOcean Droplet) for automated live trading, you must secure the environment.

## 1. Environment Secrets
Do NOT copy `.env` files using insecure protocols. Use a secure vault (e.g. AWS Secrets Manager, GitHub Secrets) or inject them at deployment time.

Required production variables:
```bash
QUANT_ENCRYPTION_KEY="<generate a strong random string>"
QUANT_WHITELISTED_IPS="<your-vps-static-ip>,<your-home-ip>"
```

## 2. Static IP Management
Brokers often require API calls to originate from whitelisted IP addresses.
- Assign an Elastic IP / Static IP to your VPS.
- Add this IP to your Upstox/Dhan developer console.
- Add this IP to `QUANT_WHITELISTED_IPS` for internal software enforcement.

## 3. Pre-flight Checks
On the VPS, verify the setup:
```bash
python -m auth.cli validate
python -m auth.cli status
```

*Ensure all infrastructure checks pass `[OK]`.*
