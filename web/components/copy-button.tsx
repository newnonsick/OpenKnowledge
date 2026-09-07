"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";

export function CopyButton({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const timerRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
    }
  }, []);

  useEffect(() => {
    setCopied(false);
    setFailed(false);
  }, [value]);

  const armReset = () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
    }
    timerRef.current = window.setTimeout(() => { setCopied(false); setFailed(false); }, 1600);
  };

  const copy = async () => {
    if (!value) {
      return;
    }
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setFailed(false);
      armReset();
    } catch {
      setCopied(false);
      setFailed(true);
      armReset();
    }
  };

  return (
    <button aria-label={copied ? `${label} copied` : failed ? `${label} failed to copy` : label} className="secret-copy-button" data-copied={copied} disabled={!value} onClick={() => void copy()} title={failed ? "Copy failed — select the text manually" : label} type="button">
      {copied ? <Check aria-hidden="true" size={15} /> : <Copy aria-hidden="true" size={15} />}
      <span aria-live="polite" className="visually-hidden">{copied ? "Copied" : failed ? "Copy failed" : ""}</span>
    </button>
  );
}
