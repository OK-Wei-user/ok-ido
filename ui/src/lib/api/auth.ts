import { get, post } from "./fetch";
import type {
  User,
  LoginParams,
  LoginResponse,
  RegisterParams,
  RefreshTokenParams,
  RefreshTokenResponse,
  ChangePasswordParams,
} from "./types";

export const authApi = {
  register: (params: RegisterParams): Promise<User> => {
    return post<User>("/auth/register", params);
  },

  login: async (params: LoginParams): Promise<{ access_token: string; refresh_token: string; user: User }> => {
    const response = await post<LoginResponse>("/auth/login", params);
    return {
      access_token: response.access_token,
      refresh_token: response.refresh_token,
      user: {
        user_id: response.user_id,
        username: response.username,
        phone: "",
        role: response.role as "admin" | "user",
        is_active: true,
      },
    };
  },

  getCurrentUser: (): Promise<User> => {
    return get<User>("/auth/me");
  },

  refreshToken: (params: RefreshTokenParams): Promise<RefreshTokenResponse> => {
    return post<RefreshTokenResponse>("/auth/refresh", params);
  },

  changePassword: (params: ChangePasswordParams): Promise<void> => {
    return post<void>("/auth/change-password", params);
  },
};
