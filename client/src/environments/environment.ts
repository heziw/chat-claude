// Production environment. apiBaseUrl should be overridden at deploy time
// (e.g. via a reverse proxy serving the gateway on the same origin, or by
// rebuilding with a different value) for the air-gapped on-prem deployment.
export const environment = {
  production: true,
  apiBaseUrl: 'http://localhost:3000'
};
