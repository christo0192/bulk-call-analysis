import { expect, it } from 'vitest';
import { bytes } from './Cards';
import { formatCount } from '../format';

it('formats one byte as a minimum kilobyte', () => expect(bytes(1)).toBe('0.0 KB'));
it('formats kilobytes with one decimal', () => expect(bytes(2048)).toBe('2.0 KB'));
it('formats megabytes with one decimal', () => expect(bytes(2 * 1024 ** 2)).toBe('2.0 MB'));
it('formats gigabytes with one decimal', () => expect(bytes(3 * 1024 ** 3)).toBe('3.0 GB'));
it('formats zero as zero', () => expect(formatCount(0, 'en-US')).toBe('0'));
it('formats thousands with separators', () => expect(formatCount(11058, 'en-US')).toBe('11,058'));
it('formats millions with en-US separators', () =>
  expect(formatCount(1000000, 'en-US')).toBe('1,000,000'));
it('formats millions with en-IN separators', () =>
  expect(formatCount(1000000, 'en-IN')).toBe('10,00,000'));
