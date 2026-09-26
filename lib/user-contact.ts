// Mirrors native/user_contact.py `kind` so the Save button matches what the backend accepts.
export type ContactStatus = {
  schema: 1;
  contact: string | null;
  kind: 'email' | 'slack' | null;
  savedAt: number | null;
  canChange: boolean;
};

const email = /^[^\s@]{1,64}@[^\s@]{1,189}\.[^\s@.]{2,63}$/u;
const slack = /^@[^\s@][^@]{0,79}$/u;

export function contactKind(value: string): 'email' | 'slack' | null {
  if (!value || value.length > 254 || /[\u0000-\u001f\u007f]/.test(value))
    return null;
  if (email.test(value)) return 'email';
  if (slack.test(value) && value === value.trim()) return 'slack';
  return null;
}

export function validContactStatus(value: unknown): value is ContactStatus {
  const v = value as ContactStatus | null;
  return (
    !!v &&
    typeof v === 'object' &&
    v.schema === 1 &&
    typeof v.canChange === 'boolean' &&
    (v.contact === null
      ? v.kind === null
      : typeof v.contact === 'string' && contactKind(v.contact) === v.kind) &&
    (v.savedAt === null ||
      (typeof v.savedAt === 'number' && Number.isFinite(v.savedAt)))
  );
}
