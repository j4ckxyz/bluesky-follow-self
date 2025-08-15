#!/usr/bin/env python3
"""
Bluesky Self-Follow Tool (improved)

- Accepts either an AT handle (username.domain) or a DID (did:...)
- Resolves handle -> DID (DNS _atproto TXT, /.well-known/atproto-did, fallback to public resolver)
- Resolves DID -> DID Document to find the user's PDS (#atproto_pds service entry)
- Attempts login against the default service first, and if that fails tries the user's PDS
- Creates an app.bsky.graph.follow record pointing at your own DID

Run: python main.py
"""

from __future__ import annotations
import sys
import time
import json
import getpass
from datetime import datetime, timezone
from typing import Optional, Tuple

try:
    from atproto import Client
except ImportError:
    print("Error: atproto library not found. Install it with: pip install atproto")
    sys.exit(1)

# Optional libs (dns.resolver speeds up DNS TXT resolution). If not installed, code falls back.
try:
    import dns.resolver  # type: ignore
except Exception:
    dns = None  # type: ignore

import requests

# ---------- Helpers for identity resolution ----------

def strip_at(h: str) -> str:
    return h[1:] if h.startswith("@") else h

def maybe_assume_bsky(handle: str) -> str:
    """
    If user provided a single token like 'alice' we assume alice.bsky.social.
    This is a convenience — many people type just a username by mistake.
    """
    if handle.startswith("did:"):
        return handle
    if "." not in handle:
        return f"{handle}.bsky.social"
    return handle

def resolve_handle_via_dns(handle: str, timeout: float = 3.0) -> Optional[str]:
    """Try DNS TXT _atproto.<domain> for a did=... record"""
    if dns is None:
        return None
    try:
        name = strip_at(handle).split(".", 1)[1] if handle.count(".") >= 1 and not handle.startswith("did:") else strip_at(handle)
    except Exception:
        name = strip_at(handle)
    query = f"_atproto.{name}"
    try:
        answers = dns.resolver.resolve(query, "TXT", lifetime=timeout)
    except Exception:
        return None
    for ans in answers:
        text = ans.to_text().strip()
        # Some resolvers return quoted strings, remove outer quotes
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        if text.startswith("did="):
            return text.split("=", 1)[1].strip()
    return None

def resolve_handle_via_well_known(handle: str, timeout: float = 4.0) -> Optional[str]:
    """
    Fetch https://<domain>/.well-known/atproto-did
    """
    raw = strip_at(handle)
    # get domain piece (username.domain => domain)
    if "/" in raw:
        # defensive
        domain = raw.split("/", 1)[0].split(".", 1)[1] if "." in raw else raw
    else:
        domain = raw.split(".", 1)[1] if "." in raw else raw
    if not domain:
        return None
    url = f"https://{domain}/.well-known/atproto-did"
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.text.strip()
    except Exception:
        return None
    return None

def resolve_handle_public_api(handle: str, timeout: float = 4.0) -> Optional[str]:
    """
    Use a public resolver to ask the network for the DID for <handle>.
    This uses the well-known /xrpc/com.atproto.identity.resolveHandle on a public host.
    This is a fallback if DNS/.well-known didn't succeed.
    """
    try:
        # use bsky.social as a public resolver (most clients do this)
        url = "https://bsky.social/xrpc/com.atproto.identity.resolveHandle"
        params = {"handle": strip_at(handle)}
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            return data.get("did")
    except Exception:
        return None
    return None

def resolve_handle_to_did(handle: str) -> Optional[str]:
    """
    Try: DNS TXT -> well-known -> public resolver
    Returns DID string or None
    """
    handle = strip_at(handle)
    # attempt DNS
    did = None
    if dns is not None:
        try:
            did = resolve_handle_via_dns(handle)
        except Exception:
            did = None
    if did:
        return did
    # well-known
    did = resolve_handle_via_well_known(handle)
    if did:
        return did
    # public resolver fallback
    did = resolve_handle_public_api(handle)
    return did

def fetch_did_document(did: str) -> Optional[dict]:
    """
    For did:web: fetch domain's /.well-known/did.json
    For did:plc: call the plc.directory resolver (https://plc.directory/<did>)
    Returns parsed JSON DID document or None
    """
    if did.startswith("did:web:"):
        # did:web:example.com or did:web:sub:example.com => domain = replace ':' after prefix with '/'
        suffix = did[len("did:web:"):]
        domain = suffix.replace(":", "/")
        url = f"https://{domain}/.well-known/did.json"
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                return r.json()
        except Exception:
            return None
        return None
    elif did.startswith("did:plc:") or did.startswith("did:plc"):
        # PLC DID resolution via the PLC directory service
        try:
            url = f"https://plc.directory/{did}"
            r = requests.get(url, timeout=6)
            if r.status_code == 200:
                return r.json()
        except Exception:
            return None
        return None
    else:
        # Unknown DID method - try universal resolver or return None
        try:
            url = f"https://uniresolver.io/1.0/identifiers/{did}"
            r = requests.get(url, timeout=6)
            if r.status_code == 200:
                payload = r.json()
                # Some universal resolvers embed the DID doc under 'didDocument'
                if isinstance(payload, dict) and "didDocument" in payload:
                    return payload["didDocument"]
        except Exception:
            return None
        return None

def extract_pds_from_did_doc(did_doc: dict, did: str) -> Optional[str]:
    """
    Look for the service entry with id ending '#atproto_pds' or service.id == '#atproto_pds'
    Return the serviceEndpoint (string) or None.
    """
    svc = did_doc.get("service") or did_doc.get("services") or []
    if not svc:
        return None
    for s in svc:
        sid = s.get("id", "")
        if sid.endswith("#atproto_pds") or sid == "#atproto_pds" or sid == f"{did}#atproto_pds":
            ep = s.get("serviceEndpoint") or s.get("serviceEndpointURL") or s.get("serviceEndpoint")
            if isinstance(ep, str) and ep:
                return ep.rstrip("/")
    # fallback: some docs put serviceEndpoint in different shapes; try first service with an https endpoint
    for s in svc:
        ep = s.get("serviceEndpoint") or s.get("serviceEndpointURL")
        if isinstance(ep, str) and ep.startswith("http"):
            return ep.rstrip("/")
    return None

# ---------- ATProto login & follow functions ----------

def try_login(client_base_url: Optional[str], identifier: str, app_password: str) -> Tuple[Optional[Client], Optional[dict]]:
    """
    Try to login with atproto.Client.
    client_base_url: if provided, instantiate Client(client_base_url), otherwise default Client()
    identifier: handle or DID depending on PDS rules — we will pass the same thing the SDK expects (usually handle)
    returns (client_instance or None, profile dict or None)
    """
    try:
        if client_base_url:
            # SDK expects full xrpc base URL (examples use '/xrpc' suffix)
            if not client_base_url.endswith("/xrpc"):
                base = client_base_url.rstrip("/") + "/xrpc"
            else:
                base = client_base_url
            client = Client(base)
        else:
            client = Client()
        # login either returns a profile object or raises
        profile = client.login(identifier, app_password)
        # profile might be a model object; try to coerce to dict for convenience
        try:
            p = profile.__dict__
        except Exception:
            try:
                p = dict(profile)
            except Exception:
                p = profile
        return client, p
    except Exception as e:
        # caller inspects the exception
        return None, {"error": str(e)}

def login_flow(raw_input_handle: str) -> Optional[Tuple[Client, str]]:
    """
    Return (client, user_did) or None if failed.
    Implements: quick try default -> on 401 try to resolve handle -> find PDS -> retry
    """
    # Normalize input
    input_val = raw_input_handle.strip()
    if not input_val:
        print("Handle/DID empty.")
        return None

    # If user gave a bare username, helpfully assume .bsky.social
    if not input_val.startswith("did:") and "." not in input_val:
        assumed = maybe_assume_bsky(input_val)
        print(f"Note: assuming handle '{assumed}' (you typed '{input_val}'). If this is wrong, provide the full handle like 'username.example.com'.")
        input_val = assumed

    identifier_for_login = input_val  # usually the handle

    app_password = getpass.getpass("Enter your Bluesky app password: ").strip()
    if not app_password:
        print("No password provided.")
        return None

    # 1) Quick attempt against default service (many users are on bsky.social)
    print("Trying quick login against the default public service (bsky.app / bsky.social)...")
    client, profile_or_err = try_login(None, identifier_for_login, app_password)
    if client:
        # success. find DID
        user_did = getattr(client.me, "did", None) or (profile_or_err.get("did") if isinstance(profile_or_err, dict) else None)
        if not user_did:
            # try the profile object
            try:
                user_did = profile_or_err.get("did") if isinstance(profile_or_err, dict) else None
            except Exception:
                user_did = None
        print("✓ Logged in (default service).")
        return client, user_did or identifier_for_login

    # If not client, figure out error cause and attempt resolution
    err_text = profile_or_err.get("error", "<unknown>") if isinstance(profile_or_err, dict) else str(profile_or_err)
    print(f"Quick login failed: {err_text}")

    # 2) Resolve handle -> DID (DNS / well-known / public resolver)
    if input_val.startswith("did:"):
        did = input_val
    else:
        print("Resolving handle to DID (DNS /.well-known / public resolver)...")
        did = resolve_handle_to_did(input_val)

    if not did:
        print("Could not resolve handle to a DID automatically. Make sure the handle is correct and the domain exposes the _atproto TXT or /.well-known/atproto-did. You can also supply a DID directly (did:web:... or did:plc:...).")
        return None

    print(f"Resolved handle -> DID: {did}")

    # 3) Fetch DID document and find PDS
    did_doc = fetch_did_document(did)
    if not did_doc:
        print("Could not fetch DID document to discover the PDS. For did:web this is at /.well-known/did.json; for did:plc we query the plc.directory resolver.")
        return None

    pds_host = extract_pds_from_did_doc(did_doc, did)
    if not pds_host:
        print("DID document did not contain an #atproto_pds entry. Without a PDS endpoint we cannot log in directly. Here is the DID document (truncated):")
        print(json.dumps(did_doc, indent=2)[:2000])
        return None

    print(f"Found PDS host: {pds_host}  -> will try to login against that PDS.")

    # 4) Attempt login against that PDS
    client, profile_or_err = try_login(pds_host, identifier_for_login, app_password)
    if client:
        user_did = getattr(client.me, "did", None) or did
        print("✓ Logged in to user's PDS.")
        return client, user_did
    else:
        err_text = profile_or_err.get("error", "<unknown>") if isinstance(profile_or_err, dict) else str(profile_or_err)
        print(f"Login against the discovered PDS failed: {err_text}")
        print("Common causes: 1) incorrect app password (make sure you used an app/password from Settings → App Passwords), or 2) the account is on a different PDS than what's listed in the DID document (rare).")
        return None

def follow_self(client: Client, user_did: str) -> bool:
    """
    Create an app.bsky.graph.follow record where subject == your DID.
    Uses dict-based payload for compatibility.
    """
    now_iso = datetime.now(timezone.utc).astimezone().isoformat()
    payload = {
        "repo": user_did,
        "collection": "app.bsky.graph.follow",
        "record": {
            "subject": user_did,
            "createdAt": now_iso
        }
    }
    try:
        client.com.atproto.repo.create_record(payload)
        return True
    except Exception as e:
        print(f"Error while creating follow record: {e}")
        return False

# ---------- CLI / flow ----------

def confirm(prompt: str) -> bool:
    while True:
        r = input(prompt + " (y/N): ").strip().lower()
        if r in ("y", "yes"):
            return True
        if r in ("n", "no", ""):
            return False
        print("Please answer 'y' or 'n'.")

def main():
    print("Bluesky Self-Follow Tool — improved")
    print("=" * 40)
    print("You will be prompted for your handle (or DID) and an app password (NOT your account password).")
    print("If you only type a username, we assume username.bsky.social.\n")

    while True:
        handle = input("Enter your Bluesky handle (e.g., username.bsky.social) or DID (did:...): ").strip()
        if not handle:
            print("Handle cannot be empty.")
            continue

        login_result = login_flow(handle)
        if not login_result:
            print("Login sequence failed. Try again or check that you are using an app password.")
            try_again = input("Try another handle? (y/N): ").strip().lower()
            if try_again in ("y", "yes"):
                continue
            else:
                break

        client, user_did = login_result
        display_handle = strip_at(handle) if not handle.startswith("did:") else user_did

        print(f"\nAbout to create a self-follow for {display_handle} (DID: {user_did}).")
        if not confirm("Are you sure you want to proceed"):
            try:
                client.logout()
            except Exception:
                pass
            if confirm("Try another account?"):
                continue
            else:
                break

        print("Creating follow record...")
        ok = follow_self(client, user_did)
        if ok:
            print("Successfully created a follow record for yourself.")
        else:
            print("Failed to create follow record. See messages above.")

        try:
            client.logout()
        except Exception:
            pass

        if confirm("Follow another account?"):
            continue
        else:
            break

    print("Done. Goodbye.")
    sys.exit(0)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Bye.")
        sys.exit(0)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)
