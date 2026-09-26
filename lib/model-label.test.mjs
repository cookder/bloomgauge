import test from 'node:test';
import assert from 'node:assert/strict';
import { modelLabel } from './model-label.ts';

test('one readable name per model id', () => {
  for (const [id, label] of [
    ['gemma-4-26b-qat-4bit', 'Gemma 4 26B'],
    ['gpt-oss-20b', 'GPT-OSS 20B'],
    ['qwen3.6-35b-a3b-vl-mtp-mxfp8', 'Qwen3.6 35B A3B VL MTP'],
    ['nemotron-3.5-lightning', 'Nemotron 3.5 Lightning'],
    ['gemma-3n-e4b-it-bf16', 'Gemma 3n E4B IT'],
  ])
    assert.equal(modelLabel(id), label);
});

test('same-named variants in one list get their quantization, others keep the short name', async () => {
  const { distinctLabels, quantLabel } = await import('./model-label.ts');
  const ids = [
    'gemma-4-26b',
    'gemma-4-26b-8bit',
    'gemma-4-26b-qat-4bit',
    'gpt-oss-20b',
  ];
  const label = distinctLabels(ids);
  assert.deepEqual(ids.map(label), [
    'Gemma 4 26B',
    'Gemma 4 26B 8-bit',
    'Gemma 4 26B QAT 4-bit',
    'GPT-OSS 20B',
  ]);
  assert.equal(
    distinctLabels(['gemma-4-26b-qat-4bit', 'gpt-oss-20b'])(
      'gemma-4-26b-qat-4bit',
    ),
    'Gemma 4 26B',
  );
  assert.equal(quantLabel('qwen3.6-35b-a3b-vl-mtp-mxfp8'), 'MXFP8');
  assert.equal(quantLabel('gemma-3n-e4b-it-bf16'), 'BF16');
  assert.equal(quantLabel('gpt-oss-20b'), '');
});
