import { createServer } from './server.js';
import { createRealDbusClient } from './realDbusClient.js';

const PORT = Number(process.env.PORT ?? 4890);

const client = await createRealDbusClient();
const app = createServer(client);
app.listen(PORT, '127.0.0.1', () => {
  console.log(`command-center-relay listening on http://127.0.0.1:${PORT}`);
});
