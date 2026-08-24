import jsonata = require('jsonata');
const e = jsonata('1');
async function run(): Promise<void> {
  // @expect-error the three-argument form returns nothing, so there is nothing to await
  const value: number = await e.evaluate({}, undefined, () => undefined);
  void value;
}
void run;
