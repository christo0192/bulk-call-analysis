import { expect, it } from 'vitest';
import { cleanTool } from './Chat';

it('labels bq queries', () => expect(cleanTool('bq_query')).toBe('Ran a read-only query'));
it('labels dry runs', () => expect(cleanTool('bq_dry_run')).toBe('Checked the scan size'));
it('labels information schema reads', () =>
  expect(cleanTool('bq_information_schema')).toBe('Looked up table columns'));
it('labels data notes', () => expect(cleanTool('data_notes')).toBe('Read the data notes'));
it('labels brief updates', () => expect(cleanTool('brief')).toBe('Updated the brief'));
it('labels proposed jobs', () => expect(cleanTool('propose_job')).toBe('Prepared the plan'));
it('labels job status checks', () => expect(cleanTool('job_status')).toBe('Checked the job'));
it('labels sample reads', () =>
  expect(cleanTool('sample_results')).toBe('Read the sample answers'));
it('labels transcript reads', () =>
  expect(cleanTool('lead_transcript')).toBe('Read a masked transcript'));
it('labels result aggregates', () =>
  expect(cleanTool('result_aggregates')).toBe('Summarised the results'));
it('strips app tool prefixes', () =>
  expect(cleanTool('mcp__app__write_file')).toBe('Wrote a file'));
it('strips bq tool prefixes', () =>
  expect(cleanTool('mcp__bq__bq_query')).toBe('Ran a read-only query'));
it('uses a safe label for unknown tools', () =>
  expect(cleanTool('unknown_tool')).toBe('unknown_tool'));
it('uses a safe label for missing tools', () => expect(cleanTool()).toBe('Used a tool'));
