import jsonata = require('jsonata');
const opts: jsonata.JsonataOptions = {
  recover: true,
  RegexEngine: RegExp,
  timeout: 1000,
  stack: 100,
  sequence: 10,
};
void jsonata('1', opts);
void jsonata('1', {});
void jsonata('1');
