import type { ReactNode } from 'react';

type SectionPanelProps = {
  children: ReactNode;
  className?: string;
};

export default function SectionPanel({ children, className }: SectionPanelProps) {
  return <section className={className ? `section-panel ${className}` : 'section-panel'}>{children}</section>;
}
