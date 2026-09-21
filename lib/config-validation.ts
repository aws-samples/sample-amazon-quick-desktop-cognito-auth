import { isIP } from 'net';

export function parseAllowedCidrs(raw: unknown): string[] | undefined {
  if (raw === undefined || raw === null || raw === '') return undefined;
  let values: unknown = raw;
  if (typeof raw === 'string') {
    try { values = JSON.parse(raw); }
    catch { throw new Error('allowedCidrs must be a JSON array of IPv4/IPv6 CIDRs.'); }
  }
  if (!Array.isArray(values) || values.some((value) => typeof value !== 'string')) {
    throw new Error('allowedCidrs must be an array of IPv4/IPv6 CIDR strings.');
  }
  const cidrs = values.map((value: string) => value.trim());
  for (const cidr of cidrs) {
    const parts = cidr.split('/');
    const family = isIP(parts[0]);
    if (parts.length !== 2 || !family || parts[0].includes('%') || !/^\d+$/.test(parts[1]) ||
        Number(parts[1]) > (family === 4 ? 32 : 128)) {
      throw new Error(`Invalid CIDR: ${cidr}`);
    }
  }
  return [...new Set(cidrs)];
}

export function validateNetworkOptions(options: {
  enableCloudFront: boolean; enableWaf: boolean; allowedCidrs?: string[];
}): void {
  if (options.enableCloudFront && options.allowedCidrs?.length && !options.enableWaf) {
    throw new Error('allowedCidrs requires enableWaf=true when CloudFront is enabled; otherwise the allowlist would not be enforced.');
  }
  if (options.enableWaf && options.allowedCidrs?.some((cidr) => Number(cidr.split('/')[1]) === 0)) {
    throw new Error('AWS WAF does not support /0 CIDRs. Omit allowedCidrs to allow all client addresses.');
  }
}
