import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';
// Tailwind v3-compatible class merging, shared by the preinstalled component library.
export function cn(...inputs: ClassValue[]) { return twMerge(clsx(inputs)); }
