/** Row shape of the `users` table (see shared/CONTRACTS.md). */
export interface UserEntity {
  id: string;
  sso_subject: string;
  display_name: string;
  role: string;
  created_at: Date;
}
