# Homelab Infrastructure Core - Orchestration Handbook
This repository houses the declarative, GitOps-driven Immutable Infrastructure configuration for your personal home server cluster.
Every single service deployment—from edge-routing, authentication, dual-stack recursive DNS routing, automatic threat mitigation, to analytical log parsing—is automated using a GitLab CI/CD pipeline targeting a bare-metal Docker runner.
## 🗺️ Architectural Mapping
```
                                  +---------------------------------------------+
                                  |                 THE INTERNET                |
                                  +---------------------------------------------+
                                                         | (Cloudflare DNS-01)
                                                         v
                                              +---------------------+
                                              |     Port 80/443     |
                                              +---------------------+
                                                         |
                                                         v
  +-------------------------------------------------- rpi5 Node --------------------------------------------------+
  |                                                      |                                                        |
  |                                                      v                                                        |
  |                                           +---------------------+                                             |
  |                                           |     Traefik v3      |<==============+                             |
  |                                           | (SSL Term / Router) |               |                             |
  |                                           +---------------------+               | (Security Checks)           |
  |                                            /         |         \                |                             |
  |                                           /          |          \               v                             |
  |                                          v           v           v       +--------------+                     |
  |                                     [AdGuard]     [Auth]    [InfluxDB]   |  CrowdSec    |                     |
  |                                   (Static IPs)  (Forward)   [Grafana]    |  Local Agent |                     |
  |                                         ^            |                   +--------------+                     |
  |                                         |            |                          |                             |
  +-----------------------------------------|------------|--------------------------|-----------------------------+
                                            |            |                          |
                                (Local DNS) |            | (ForwardAuth Request)    | (Stream Alerts)
                                            |            v                          v
  +-----------------------------------------|------- Unraid Host (param) -----------|-----------------------------+
  |                                         |            |                          |                             |
  |                                         |            v                          v                             |
  |                                         |     +--------------+           +--------------+                     |
  |                                         +====>| Downstream   |           | Central LAPI |                     |
  |                                               | Services     |           | Engine & DB  |                     |
  |                                               +--------------+           +--------------+                     |
  +---------------------------------------------------------------------------------------------------------------+
```
## 🛠️ Section 1: Host Machine Provisioning & Bootstrapping

Follow the instructions below depending on whether you are initializing an **ARM64 (Raspberry Pi 5)** or **x86_64 (Ubuntu Server)** node.

### 🔑 Core Prerequisite: One-Time Host Bootstrapping
Because this repository utilizes a local-connection Ansible loop executed directly via a bare-metal GitLab Runner shell executor, the target host requires a brief manual bootstrapping phase so it can manage its own lifecycle. 

Execute these commands directly on the host machine terminal:

1. **Install Base Execution Engines & Dependencies**:
   ```
   bash
   sudo apt update
   sudo apt install -y ansible python3-pip python3-docker
   ```

2. **Configure Passwordless Sudo for the CI Runner**:
The `gitlab-runner` system user must be allowed to elevate privileges without an interactive password prompt to execute system updates (`apt upgrade`) and Docker space sweeps (`docker prune`):
   ```
   bash
   echo "gitlab-runner ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/gitlab-runner
   ```



---

### Option A: Raspberry Pi 5 Setup (ARM64)
 1. **Flash OS**: Flash **Raspberry Pi OS Lite (64-bit)** using the Raspberry Pi Imager. Ensure you configure SSH and user credentials during the imaging process.
 2. **Perform System Upgrades & Packages Installation**:
   ```bash
   sudo apt update && sudo apt upgrade -y
   sudo apt install -y curl git ufw jq net-tools wireguard-tools
   
   ```
 3. **Install the Docker Engine**:
   ```bash
   curl -fsSL [https://get.docker.com](https://get.docker.com) -o get-docker.sh
   sudo sh get-docker.sh
   # Add your system user to the docker daemon group
   sudo usermod -aG docker $USER
   newgrp docker
   
   ```
 4. **Enable IPv6 Kernel Forwarding (Crucial for dual-stack)**:
   Add these configurations to /etc/sysctl.conf:
   ```ini
   net.ipv6.conf.all.forwarding=1
   net.ipv6.conf.default.forwarding=1
   
   ```
   Apply the adjustments live:
   ```bash
   sudo sysctl -p
   
   ```
### Option B: Ubuntu Server Setup (x86_64 / Intel / AMD)
If you migrate to standard x64 hardware instead of the Raspberry Pi 5, the steps are practically identical, with two crucial architecture changes:
 1. **Base Dependencies**:
   ```bash
   sudo apt update && sudo apt upgrade -y
   sudo apt install -y curl git ufw jq software-properties-common
   
   ```
 2. **Install Docker Engine**:
   Follow standard Ubuntu Docker Engine installation procedures:
   ```bash
   sudo mkdir -p /etc/apt/keyrings
   curl -fsSL [https://download.docker.com/linux/ubuntu/gpg](https://download.docker.com/linux/ubuntu/gpg) | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
   echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] [https://download.docker.com/linux/ubuntu](https://download.docker.com/linux/ubuntu) $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
   sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
   sudo usermod -aG docker $USER
   
   ```
 3. **Architecture Match adjustments (IaC Impact)**:
   Inside core/auth/docker-compose.yml, the traefik-forward-auth services use a custom Dockerfile built explicitly for ARM64:
   ```yaml
   build:
     context: [https://github.com/thomseddon/traefik-forward-auth.git#master](https://github.com/thomseddon/traefik-forward-auth.git#master)
     dockerfile: Dockerfile.arm64
   
   ```
   **If deploying on Ubuntu x86_64**, modify the service declarations to use the standard, pre-compiled official image instead:
   ```yaml
   image: thomseddon/traefik-forward-auth:2
   # Remove the build block entirely
   
   ```
## 🔑 Section 2: Secrets Harvesting Guide
Before running the GitLab Pipeline, you must gather your specific tokens, keys, and secrets. Follow these steps to secure each required value.
### 1. Cloudflare DNS API Key & Email
 * **What it does**: Allows Traefik to resolve wildcard cert DNS-01 challenges (*.example.com, *.local.example.com) by automatically writing dynamic validation TXT records.
 * **How to get it**:
   1. Login to your Cloudflare Dashboard.
   2. Navigate to **My Profile ➔ API Tokens**.
   3. Click **Create Token** ➔ Select **Edit Zone DNS** template.
   4. Scope the token to your specific domain zone (example.com).
   5. Alternatively, copy your **Global API Key** (requires password validation). We recommend the scoped API Token for security.
 * **Variable Names**: CF_API_EMAIL (your Cloudflare login email) and CF_API_KEY (your API token).
### 2. Google OAuth Credentials (for Forward Auth)
 * **What it does**: Secures your private subdomains. Only authorized Google accounts (whitelisted in your options.conf files) will be permitted to pass through Traefik.
 * **How to get it**:
   1. Head to the Google Cloud Console.
   2. Create a new project (e.g., Homelab Auth).
   3. Go to **APIs & Services ➔ OAuth consent screen**. Set user type to **External**, fill in the application name, and save.
   4. Go to **Credentials ➔ Create Credentials ➔ OAuth client ID**.
   5. Choose **Web Application** as application type.
   6. Add your Authorized Redirect URIs:
     * https://auth.example.com/_oauth
     * https://auth.local.example.com/_oauth
   7. Copy your **Client ID** and **Client Secret**.
 * **Variable Names**: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.
### 3. Forward Auth Session Key
 * **What it does**: Generates encrypted browser session cookies to track authenticated users across your subdomains.
 * **How to generate one**:
   Run this command in any terminal to generate a cryptographically secure 32-character hex key:
   ```bash
   openssl rand -hex 16
   
   ```
 * **Variable Name**: FORWARD_AUTH_SECRET.
### 4. CrowdSec Inter-Node Password (Agent to LAPI Auth)
 * **What it does**: Authenticates the lean log-parsing CrowdSec container running on the Pi 5 to the remote central database on param.
 * **How to get it**:
   1. Log into your central Unraid machine (param).
   2. Run the following command inside your master CrowdSec container context to register the Pi 5 agent:
     ```bash
     docker exec -it crowdsec cscli agents add rpi5
     
     ```
   3. This command will output a randomly generated password. Copy it.
 * **Variable Name**: CROWDSEC_INTER_NODE_PASSWORD.
### 5. CrowdSec Bouncer API Key (Traefik Integration)
 * **What it does**: Authorizes the Traefik CrowdSec bouncer plugin to read blocklists from your remote LAPI.
 * **How to get it**:
   1. Log into your central Unraid machine (param).
   2. Generate a custom API key for the Traefik plugin bouncer:
     ```bash
     docker exec -it crowdsec cscli bouncers add rpi5-bouncer
     
     ```
   3. Copy the returned API key exactly.
 * **Variable Name**: CROWDSEC_API_KEY.
### 6. MaxMind Account ID & License Key
 * **What it does**: Downloads updated GeoLite2 country/city database files every 24 hours so your Traefik metrics dashboard can map threat locations geographically.
 * **How to get it**:
   1. Sign up for a free account at MaxMind GeoLite2.
   2. Go to **My License Keys** ➔ **Generate New License Key**.
   3. Save your **Account ID** and the generated **License Key** securely.
 * **Variable Names**: MAXMIND_ACCOUNT_ID and MAXMIND_LICENSE_KEY.
### 7. InfluxDB Setup Parameters
 * **What it does**: Provisions your local InfluxDB time-series datastore and authorizes traefikgraf to push metrics.
 * **How to generate**:
   1. Choose an admin username (e.g., dhinadhindha).
   2. Generate a strong admin password (INFLUXDB_INIT_PASSWORD).
   3. Generate a safe admin access token for the INFLUX_TOKEN value:
     ```bash
     openssl rand -base64 48
     
     ```
 * **Variable Names**: INFLUXDB_INIT_USERNAME, INFLUXDB_INIT_PASSWORD, and INFLUX_TOKEN.
## 🚀 Section 3: GitLab CI/CD Variables Setup
To link your Git repository to your actual Raspberry Pi 5 hardware, you must register a pipeline runner and configure your environment variables.
### Step 1: Install the GitLab Runner on your Node
 1. Add the official GitLab repository:
   ```bash
   curl -L "[https://packages.gitlab.com/install/repositories/runner/gitlab-runner/script.deb.sh](https://packages.gitlab.com/install/repositories/runner/gitlab-runner/script.deb.sh)" | sudo bash  
   ```
 2. Install the runner binary:
   ```bash
   sudo apt install gitlab-runner -y   
   ```
 3. Set the runner system user to have complete control over the local Docker daemon:
   ```bash
   sudo usermod -aG docker gitlab-runner
   sudo systemctl restart gitlab-runner
   ```
 4. Register your runner:
   Go to your GitLab Repository ➔ **Settings ➔ CI/CD ➔ Runners** and create a project runner. Set the runner tags to: **rpi5**.
   Execute the registration command provided in the GitLab UI on your host system terminal:
   ```bash
   sudo gitlab-runner register
   ```
   * *Executor selection:* Choose **shell** to give the runner direct access to deploy your compose stacks natively.
### Step 2: Configure CI/CD Variables
Go to **Settings ➔ CI/CD ➔ Variables** and add the following keys exactly as listed below. Mark all sensitive keys as **Masked** so they are stripped out of public build logs:

| Variable Key | Value Source | Masked? |
| :--- | :--- | :--- |
| CF_API_EMAIL | Cloudflare Username | No |
| CF_API_KEY | Cloudflare API Token / Key | **Yes** |
| FORWARD_AUTH_SECRET | 32-Char Auth Hex Key | **Yes** |
| GOOGLE_CLIENT_ID | Google Console Client ID | No |
| GOOGLE_CLIENT_SECRET | Google Console Client Secret | **Yes** |
| CROWDSEC_INTER_NODE_PASSWORD | Central LAPI Agent Passphrase | **Yes** |
| CROWDSEC_API_KEY | Central LAPI Bouncer API Key | **Yes** |
| CAPTCHA_SITE_KEY | Recaptcha Site Key (Optional) | No |
| CAPTCHA_SECRET_KEY | Recaptcha Secret Key (Optional) | **Yes** |
| INFLUXDB_INIT_USERNAME | Admin Username for InfluxDB | No |
| INFLUXDB_INIT_PASSWORD | Admin Password for InfluxDB | **Yes** |
| INFLUX_TOKEN | Generated All-Access Token | **Yes** |
| MAXMIND_ACCOUNT_ID | MaxMind Account ID Number | No |
| MAXMIND_LICENSE_KEY | MaxMind Licencing Token | **Yes** |
| ABUSEIP_KEY | AbuseIPDB verification key | **Yes** |
| `MAINTENANCE_TRIGGER` | Set to `true` inside Scheduled Tasks pipeline loops | No |

## 📂 Section 4: Project Directory Structure
Your repository is structured into isolated service domains alongside the Ansible orchestration assets:
```
homelab-infrastructure/
├── .gitlab-ci.yml              # GitLab continuous validation and deployment pipeline
├── renovate.json               # Automatic dependency upgrade policy schedules
├── .gitleaksignore             # Historical false-positive scanner exceptions
├── core/
│   ├── auth/                   # Forward OAuth validation systems
│   │   ├── docker-compose.yml
│   │   ├── options.local.conf  # Dynamic rule definitions for local domains
│   │   └── options.public.conf # Dynamic rule definitions for WAN domains
│   ├── dns/                    # DNS-over-HTTPS (DoH) / SSL Certificate Dumper
│   │   └── docker-compose.yml
│   ├── security/               # Distributed Intrusion Prevention Engine (CrowdSec)
│   │   ├── acquis.yaml         # Log tracking plan
│   │   └── docker-compose.yml
│   ├── traefik/                # Boundary Reverse Proxy & TLS Termination
│   │   ├── config.yml          # Dynamic routing endpoints and whitelists
│   │   ├── docker-compose.yml  # Includes optimized logrotate sidecar tracking
│   │   └── traefik.yml         # Static reverse proxy config rules
│   └── traefikstats/           # Logs parsing engine, geo-lookup, and dashboard
│       └── docker-compose.yml
└── provisioning/
    └── ansible/                # Automated Host OS Pruning & Patching
        ├── files/
        │   ├── 20auto-upgrades        # Background cron timing intervals for apt
        │   └── 50unattended-upgrades  # Unattended automated security rules
        ├── inventory.ini              # Local bypass runtime targets
        └── playbook.yml               # Comprehensive safe system upgrade playbook

```
## 🏁 Section 5: Executing the Fresh Deployment
Once your GitLab runner is online and your variables are fully declared:
 1. Push your changes directly to your default repository branch:
   ```bash
   git add .
   git commit -m "feat: infrastructure deployment trigger"
   git push origin main
   
   ```
 2. The GitLab CI/CD runner will pick up the job and execute the **Scorched Earth Deployment Protocol**:
   * It forcefully wipes all running Docker container processes to guarantee clean ports.
   * It drops and recreates the reverse_proxy external bridge network canonically.
   * It brings up core/dns (AdGuard Home + Unbound) first to securely lock down its static IP spaces (172.20.0.3 and fd01:1234:10::3).
   * It compiles your Forward Authentication configuration configurations on the fly.
   * It deploys your reverse proxy (Traefik), authentication layers, analytics engine, and CrowdSec integrations in deterministic sequence.
## 🤖 Section 6: Automated Host Maintenance & GitOps Reboots

To keep your host OS clean, updated, and fast without introducing manual shell configuration drift, a scheduled pipeline runs an interactive Ansible playbook targeting the host directly using a fast, non-networked local loop connector (`ansible_connection=local`).

### Maintenance Pipeline Strategy

The playbook splits system maintenance into separate operational layers:

1. **Continuous Background Security Patches:** Configures `unattended-upgrades` to silently evaluate and download daily zero-day security packages.
2. **Weekly Full Upgrades:** Runs a weekly non-disruptive safe upgrade on all non-security operating system binaries.
3. **Aggressive Pruning Loop:** Runs an automated `apt autoremove` and a targeted `docker prune` execution block that safely targets dangling build-cache layers and orphaned container tags while explicitly preserving persistent data volume paths.
4. **Delayed-Fuse Graceful Reboots:** If the system upgrades dictate that a hardware reboot is necessary to apply modifications (e.g., kernel bumps), the playbook queues a detached `shutdown -r +1` command. This ensures the command returns a successful exit status of `0` immediately, allowing the GitLab pipeline to close out completely **GREEN** before the host gracefully power-cycles itself 60 seconds later.

### Tracking Reports

The metric results are surfaced cleanly within your **GitLab UI ➔ Build ➔ Jobs** console logs following every run:

```text
TASK [Surface host maintenance metric report summary] **************************
ok: [localhost] => {
    "msg": [
        "Host Processing Complete: localhost",
        "Apt Changes Applied: True",
        "Docker Layers Purged: 12 layers dropped",
        "System Reboot Required: FALSE"
    ]
}

```

## 📝 Section 7: Post-Deployment Runbooks
### 🔒 Enforcing DNS Encryption in AdGuard Home
Using our automated certs-dumper setup, the absolute millisecond Traefik successfully validates and writes your SSL certificates to /acme.json, they are automatically exported as flat files directly inside /home/user/docker/dns/adguard_config/certs/.
To enable secure DNS-over-TLS (DoT) and DNS-over-HTTPS (DoH):
 1. Log into your AdGuard Web Portal at https://clusterguard.local.example.com (or http://172.20.0.3 internally during setup).
 2. Go to **Settings ➔ Encryption settings**.
 3. Toggle **Enable Encryption** to active.
 4. Set **Server name** to: clusterguard.example.com.
 5. Scroll down to the **Certificates** paths block and provide these exact inside-container mount configurations:
   * **Path to certificate file**:
     ```text
     /opt/AdGuardHome/data/certs/cert.pem
     
     ```
   * **Path to private key file**:
     ```text
     /opt/AdGuardHome/data/certs/key.pem
     
     ```
 6. Click **Save configuration**.
 7. Your AdGuard server will instantly transition to a secure DNS profile without needing any container restarts.
### 🐛 Troubleshooting SSL Wildcard Verification Errors
If your subdomain pages return a **Traefik 404** or **Untrusted Certificate Warning**, check your logs for the ACME validation sequence:
```bash
docker logs traefik --tail 100 -f
```
 * **Common Cause 1: Broken JSON in acme.json**
   If you had failed certification requests in the past, your acme.json might contain corrupted validation states. To safely reset it:
   ```bash
   sudo systemctl stop gitlab-runner # Stop active pipelines
   docker stop traefik
   sudo echo "{}" > /home/user/docker/traefik/data/acme.json
   sudo chmod 600 /home/user/docker/traefik/data/acme.json
   docker start traefik
   ```
 * **Common Cause 2: Split-Horizon DNS Failure**
   If Traefik cannot resolve the DNS-01 verification TXT records, check your Cloudflare API validation status. Ensure that your token possesses explicit DNS:Edit and Zone:Read privileges for your target domains.
