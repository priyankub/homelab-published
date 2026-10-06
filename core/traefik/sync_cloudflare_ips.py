#!/usr/bin/env python3
import urllib.request
import os
import re
import tempfile

# Natively resolve paths relative to where this script file lives (core/traefik/)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_CONFIG = os.path.join(BASE_DIR, "traefik.yml")
DYNAMIC_CONFIG = os.path.join(BASE_DIR, "config.yml")

def fetch_cloudflare_ips():
    # Configure a standard desktop browser User-Agent to prevent Cloudflare from blocking the request with a 403 Forbidden
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    try:
        req_v4 = urllib.request.Request("https://www.cloudflare.com/ips-v4", headers=headers)
        req_v6 = urllib.request.Request("https://www.cloudflare.com/ips-v6", headers=headers)
        
        v4 = urllib.request.urlopen(req_v4, timeout=10).read().decode().splitlines()
        v6 = urllib.request.urlopen(req_v6, timeout=10).read().decode().splitlines()
        return [ip.strip() for ip in v4 + v6 if ip.strip()]
    except Exception as e:
        print(f"CRITICAL: Failed to retrieve Cloudflare IPs: {e}")
        raise e

def inject_ips(filepath, start_tag_name, end_tag_name, raw_ips_list):
    if not os.path.exists(filepath):
        print(f"Error: {filepath} not found.")
        return False
        
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    # Define standard horizontal whitespace characters (spaces, tabs, carriage returns, and non-breaking spaces)
    hspace = r"[ \t\r\xa0]"
    
    # Check presence of both tags using highly flexible regexes that ignore indentation and trailing garbage
    start_re = rf"#{hspace}*{re.escape(start_tag_name)}"
    end_re = rf"#{hspace}*{re.escape(end_tag_name)}"
    
    if not re.search(start_re, content, re.IGNORECASE) or not re.search(end_re, content, re.IGNORECASE):
        print(f"Error: Anchor identifiers {start_tag_name}/{end_tag_name} not found in {filepath}")
        return False

    # Detect the file's line-ending style to maintain formatting consistency (CRLF vs LF)
    newline = "\r\n" if "\r\n" in content else "\n"

    # Match the block between the tags (using non-greedy matching)
    pattern = rf"({hspace}*#{hspace}*{re.escape(start_tag_name)}{hspace}*\r?\n)(.*?)({hspace}*#{hspace}*{re.escape(end_tag_name)})"

    def replacement_fn(match):
        start_line = match.group(1)
        
        # Detect the exact indentation before the '#' of the start tag line
        indent_match = re.match(rf"^({hspace}*)#", start_line)
        indent = indent_match.group(1) if indent_match else "            "
        
        # Build the dynamic list of IPs perfectly aligned with the file's native indent and line endings
        indented_ips = "".join([f"{indent}- \"{ip}\"{newline}" for ip in raw_ips_list])
        
        return f"{start_line}{indented_ips}{match.group(3)}"

    updated_content = re.sub(pattern, replacement_fn, content, flags=re.DOTALL | re.IGNORECASE)
    
    dir_name = os.path.dirname(filepath)
    fd, temp_path = tempfile.mkstemp(dir=dir_name, prefix=".tmp_")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(updated_content)
        os.replace(temp_path, filepath)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise e
        
    return content != updated_content

def main():
    print("Fetching canonical Cloudflare IP networks...")
    try:
        ips = fetch_cloudflare_ips()
    except Exception:
        with open(".sync_status", "w") as f:
            f.write("SKIP")
        return # Prevent execution chain if network fetch fails
    
    static_changed = inject_ips(STATIC_CONFIG, "KC_CF_STATIC_START", "KC_CF_STATIC_END", ips)
    dynamic_changed = inject_ips(DYNAMIC_CONFIG, "KC_CF_DYNAMIC_START", "KC_CF_DYNAMIC_END", ips)
    
    if dynamic_changed:
        print("SUCCESS: Dynamic middleware map updated.")
    if static_changed:
        print("NOTICE: Static entrypoint maps updated (requires engine reload).")
        
    with open(".sync_status", "w") as f:
        f.write("RESTART" if static_changed else "HOT_RELOAD")

if __name__ == "__main__":
    main()
