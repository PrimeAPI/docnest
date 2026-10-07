import * as React from "react";
import { cn, formatDateInput, parseDateInput } from "@/lib/utils";
import { Input } from "./input";

type DateInputProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, "type" | "value" | "onChange"> & {
  value?: string | null;
  onValueChange: (value: string) => void;
};

export const DateInput = React.forwardRef<HTMLInputElement, DateInputProps>(
  ({ value, onValueChange, className, onBlur, onKeyDown, ...props }, ref) => {
    const [text, setText] = React.useState(() => formatDateInput(value));
    const [invalid, setInvalid] = React.useState(false);
    const lastCommitted = React.useRef(value ?? "");

    React.useEffect(() => {
      lastCommitted.current = value ?? "";
      setText(formatDateInput(value));
      setInvalid(false);
    }, [value]);

    const commit = (next: string) => {
      if (next === lastCommitted.current) return;
      lastCommitted.current = next;
      onValueChange(next);
    };

    return (
      <Input
        {...props}
        ref={ref}
        type="text"
        inputMode="numeric"
        autoComplete="off"
        maxLength={10}
        placeholder="DD/MM/YYYY"
        value={text}
        aria-invalid={invalid || undefined}
        title={invalid ? "Enter a valid date as DD/MM/YYYY" : props.title}
        className={cn(invalid && "border-destructive focus-visible:ring-destructive", className)}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          setInvalid(false);
          const parsed = parseDateInput(next);
          if (parsed) commit(parsed);
        }}
        onBlur={(event) => {
          const raw = event.target.value.trim();
          if (!raw) {
            setText("");
            setInvalid(false);
            commit("");
          } else {
            const parsed = parseDateInput(raw);
            if (parsed) {
              setText(formatDateInput(parsed));
              setInvalid(false);
              commit(parsed);
            } else {
              setInvalid(true);
            }
          }
          onBlur?.(event);
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter") event.currentTarget.blur();
          onKeyDown?.(event);
        }}
      />
    );
  },
);
DateInput.displayName = "DateInput";
