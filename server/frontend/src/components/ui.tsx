/** Small shared UI primitives. */

import { useId, type ReactNode } from 'react';

export type SettingStatus = 'unsaved' | 'saved';

type SettingAdornment = {
  settingStatus?: SettingStatus;
  onSettingReset?: () => void;
  settingResetLabel?: string;
};

function FieldHeading({
  id,
  label,
  suffix,
  settingStatus,
  onSettingReset,
  settingResetLabel,
}: SettingAdornment & { id?: string; label: string; suffix?: string }) {
  return (
    <span className="mb-1 flex min-h-5 items-center justify-between gap-2">
      <label htmlFor={id} className="label mb-0">
        <span
          className={`mr-1.5 inline-block h-2 w-2 rounded-full align-middle ${
            settingStatus === 'unsaved'
              ? 'bg-warn'
              : settingStatus === 'saved'
                ? 'bg-accent'
                : 'invisible'
          }`}
          title={
            settingStatus === 'unsaved'
              ? 'Changed, not saved yet'
              : settingStatus === 'saved'
                ? 'Saved override of the factory default'
                : undefined
          }
        />
        {label}
        {suffix && <span className="text-muted/70"> ({suffix})</span>}
      </label>
      {onSettingReset && (
        <button
          type="button"
          className="rounded px-1.5 py-0.5 text-xs text-muted transition hover:bg-panelalt hover:text-ink"
          onClick={onSettingReset}
          title={settingResetLabel}
          aria-label={settingResetLabel}
        >
          &#8630;
        </button>
      )}
    </span>
  );
}

export function Card({
  title,
  children,
  className = '',
  titleClassName = 'card-title',
  actions,
}: {
  title?: string;
  children: ReactNode;
  className?: string;
  titleClassName?: string;
  actions?: ReactNode;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="mb-3 flex items-center justify-between gap-2">
          {title && <h2 className={`${titleClassName} mb-0`}>{title}</h2>}
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

type Tone = 'good' | 'warn' | 'bad' | 'idle' | 'info';

const TONE_CLASS: Record<Tone, string> = {
  good: 'border-good/40 bg-good/10 text-good',
  warn: 'border-warn/40 bg-warn/10 text-warn',
  bad: 'border-bad/40 bg-bad/10 text-bad',
  info: 'border-accent/40 bg-accent/10 text-accent',
  idle: 'border-edge bg-panelalt text-muted',
};

export function Pill({
  tone = 'idle',
  children,
  title,
}: {
  tone?: Tone;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span className={`pill ${TONE_CLASS[tone]}`} title={title}>
      <span className="h-1.5 w-1.5 rounded-full bg-current" />
      {children}
    </span>
  );
}

export function StatusRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-edge/60 py-1.5 last:border-0">
      <span className="text-xs text-muted">{label}</span>
      <span className="tabular text-sm">{value}</span>
    </div>
  );
}

export function Toggle({
  label,
  checked,
  onChange,
  hint,
  disabled,
  settingStatus,
  onSettingReset,
  settingResetLabel,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  hint?: string;
  disabled?: boolean;
} & SettingAdornment) {
  return (
    <div className={`flex items-start gap-3 py-1.5 ${disabled ? 'opacity-50' : ''}`}>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={`mt-0.5 h-5 w-9 shrink-0 rounded-full border transition
          ${checked ? 'border-accent bg-accent/60' : 'border-edge bg-panelalt'}`}
      >
        <span
          className={`block h-4 w-4 rounded-full bg-ink transition-transform
            ${checked ? 'translate-x-4' : 'translate-x-0.5'}`}
        />
      </button>
      <span className="min-w-0 flex-1">
        <span className="flex items-center justify-between gap-2 text-sm">
          <span>
            <span
              className={`mr-1.5 inline-block h-2 w-2 rounded-full ${
                settingStatus === 'unsaved'
                  ? 'bg-warn'
                  : settingStatus === 'saved'
                    ? 'bg-accent'
                    : 'invisible'
              }`}
            />
            {label}
          </span>
          {onSettingReset && (
            <button
              type="button"
              className="rounded px-1.5 py-0.5 text-xs text-muted transition hover:bg-panelalt hover:text-ink"
              onClick={onSettingReset}
              title={settingResetLabel}
              aria-label={settingResetLabel}
            >
              &#8630;
            </button>
          )}
        </span>
        {hint && <span className="block text-xs text-muted">{hint}</span>}
      </span>
    </div>
  );
}

export function NumberField({
  label,
  value,
  onChange,
  step = 1,
  min,
  max,
  hint,
  suffix,
  settingStatus,
  onSettingReset,
  settingResetLabel,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  step?: number;
  min?: number;
  max?: number;
  hint?: string;
  suffix?: string;
} & SettingAdornment) {
  const id = useId();
  return (
    <div className="block py-1.5">
      <FieldHeading {...{ id, label, suffix, settingStatus, onSettingReset, settingResetLabel }} />
      <input
        id={id}
        type="number"
        className="field tabular"
        value={Number.isFinite(value) ? value : 0}
        step={step}
        min={min}
        max={max}
        onChange={(event) => {
          const parsed = Number(event.target.value);
          if (!Number.isNaN(parsed)) onChange(parsed);
        }}
      />
      {hint && <span className="mt-1 block text-xs text-muted">{hint}</span>}
    </div>
  );
}

export function TextField({
  label,
  value,
  onChange,
  hint,
  placeholder,
  type = 'text',
  autoComplete,
  error,
  settingStatus,
  onSettingReset,
  settingResetLabel,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  hint?: string;
  placeholder?: string;
  type?: string;
  autoComplete?: string;
  error?: string;
} & SettingAdornment) {
  const id = useId();
  return (
    <div className="block py-1.5">
      <FieldHeading {...{ id, label, settingStatus, onSettingReset, settingResetLabel }} />
      <input
        id={id}
        type={type}
        className={`field ${error ? 'border-bad/70 focus:border-bad' : ''}`}
        value={value}
        placeholder={placeholder}
        autoComplete={autoComplete}
        onChange={(event) => onChange(event.target.value)}
      />
      {error ? (
        <span className="mt-1 block text-xs text-bad">{error}</span>
      ) : (
        hint && <span className="mt-1 block text-xs text-muted">{hint}</span>
      )}
    </div>
  );
}

export function SelectField<T extends string>({
  label,
  value,
  options,
  onChange,
  hint,
  settingStatus,
  onSettingReset,
  settingResetLabel,
}: {
  label: string;
  value: T;
  options: readonly { value: T; label: string }[];
  onChange: (value: T) => void;
  hint?: string;
} & SettingAdornment) {
  const id = useId();
  return (
    <div className="block py-1.5">
      <FieldHeading {...{ id, label, settingStatus, onSettingReset, settingResetLabel }} />
      <select
        id={id}
        className="field"
        value={value}
        onChange={(event) => onChange(event.target.value as T)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      {hint && <span className="mt-1 block text-xs text-muted">{hint}</span>}
    </div>
  );
}

export function Banner({ tone = 'bad', children }: { tone?: Tone; children: ReactNode }) {
  return (
    <div className={`rounded-lg border px-3 py-2 text-sm ${TONE_CLASS[tone]}`}>{children}</div>
  );
}

export function Spinner({ label = 'loading' }: { label?: string }) {
  return <p className="py-6 text-center text-sm text-muted">{label}…</p>;
}
