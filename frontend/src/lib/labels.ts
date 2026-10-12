// Plain-language labels for everything the backend expresses as codes.

export const GROUP_LABEL: Record<string, string> = {
  'region-na': 'North America',
  'region-eu': 'Europe',
  'family-pumps': 'Pumps',
  'family-compressors': 'Compressors',
  'family-chillers': 'Chillers',
  'family-conveyors': 'Conveyors',
  'cert-hv': 'High-voltage certified',
  'cert-refrigerant': 'Refrigerant certified',
  'cert-confined-space': 'Confined-space certified',
  'role-lead': 'Team lead',
}

export function groupKind(g: string): 'region' | 'family' | 'cert' | 'role' {
  if (g.startsWith('region')) return 'region'
  if (g.startsWith('cert')) return 'cert'
  if (g.startsWith('role')) return 'role'
  return 'family'
}

export const CERT_PLAIN: Record<string, string> = {
  HV: 'high-voltage',
  Refrigerant: 'refrigerant',
  'Confined space': 'confined-space',
}

export const DOC_TYPE: Record<string, string> = {
  oem_bulletin: 'Manufacturer notice',
  oem_manual: 'Manual',
  controlled_procedure: 'Company procedure',
  legacy_wi: 'Work instruction (Kyltek)',
  wiki: 'Team wiki',
  email: 'Email',
  chat: 'Team chat',
  cmms_export: 'Maintenance record',
  case_export: 'Service case',
  asset_register: 'Equipment list',
  ops_review: 'Ops review',
  record: 'Record',
}

export const REASON_ACTION: Record<string, string> = {
  'certified technician required': 'Request a certified technician',
  'parts required': 'Order parts & schedule the fix',
  'OEM campaign': 'Schedule the manufacturer fix',
  'return visit': 'Book a return visit',
  escalation: 'Escalate to the team lead',
}

export function initials(name: string) {
  return name.split(' ').map((p) => p[0]).slice(0, 2).join('').toUpperCase()
}

export function timeAgo(iso: string) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  return new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

// A short, friendly label for a source: "Manual · page 9", "Service case 00041988", ...
export function sourceTitle(label: string) {
  return label.replace(/\s*·\s*/g, ' · ')
}
