'use client';
import { CheckCircle2, CircleAlert, LoaderCircle } from 'lucide-react';

export type Warmup = { status: string; detail: string; model?: string };

export function ModelWarmup({ value }: { value?: Warmup }) {
  if (!value) return null;
  const ready = value.status === 'ready';
  const warming = value.status === 'warming';
  return (
    <p
      className={`model-warmup ${ready ? 'ready' : warming ? 'warming' : 'waiting'}`}
      role="status"
    >
      {ready ? (
        <CheckCircle2 size={16} />
      ) : warming ? (
        <LoaderCircle size={16} className="spin" />
      ) : (
        <CircleAlert size={16} />
      )}
      <span>{value.detail}</span>
    </p>
  );
}
