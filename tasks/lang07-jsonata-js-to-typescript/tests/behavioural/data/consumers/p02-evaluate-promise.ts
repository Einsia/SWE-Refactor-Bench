import jsonata = require('jsonata');
async function run(): Promise<void> {
  const value: unknown = await jsonata('a.b').evaluate({ a: { b: 1 } });
  void value;
}
void run;
