/**
 * Client-side Ed25519 request signing — the exact scheme
 * `core/registry.py::verify_agent_request` checks:
 * `sign(method + "\n" + sha256(body).hex() + "\n" + timestamp + "\n" + nonce)`.
 *
 * This only exists because `/live` plays the part of an AI shopping agent
 * for the demo — a real agent operator would sign with their own key on
 * their own infrastructure, never in a browser. `POST /api/agents/register`
 * (omitting `public_key`) generates a demo keypair server-side and returns
 * the private key exactly once, for exactly this purpose — see its
 * docstring in api/praman/api/routes_agents.py.
 */

"use client";

import { sha256, sha512 } from "@noble/hashes/sha2.js";
import * as ed from "@noble/ed25519";

ed.hashes.sha512 = sha512;

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function hexToBytes(hex: string): Uint8Array {
  const clean = hex.length % 2 ? `0${hex}` : hex;
  const out = new Uint8Array(clean.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = parseInt(clean.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

export interface SignedRequest {
  timestamp: string;
  nonce: string;
  signature: string;
}

/** Signs `body` (must be the exact JSON string that will be POSTed) for one
 * request, generating a fresh timestamp + nonce each call — a nonce may
 * never be reused (R01's replay check). */
export function signRequest(privateKeyHex: string, method: string, body: string): SignedRequest {
  const timestamp = new Date().toISOString();
  const nonce = bytesToHex(crypto.getRandomValues(new Uint8Array(16)));
  const bodyHash = bytesToHex(sha256(new TextEncoder().encode(body)));
  const message = new TextEncoder().encode(`${method}\n${bodyHash}\n${timestamp}\n${nonce}`);
  const signature = bytesToHex(ed.sign(message, hexToBytes(privateKeyHex)));
  return { timestamp, nonce, signature };
}
