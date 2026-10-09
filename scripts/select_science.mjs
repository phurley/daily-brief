import fs from 'node:fs';
import { selectScienceDigest } from '../science.mjs';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const { date, selectedIds, reasons } = selectScienceDigest(input.document, input.date, { reset: input.reset });
process.stdout.write(JSON.stringify({ date, selectedIds, reasons }));
