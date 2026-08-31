'use client'

import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { authApi, setToken, removeToken, getToken, setRefreshToken, getRefreshToken, onAuthExpired, API_CONFIG } from '@/lib/api'
import type { User, LoginParams, RegisterParams } from '@/lib/api'

const USER_KEY = 'auth_user'

interface AuthContextType {
  user: User | null
  isLoading: boolean
  isAuthenticated: boolean
  login: (params: LoginParams) => Promise<void>
  register: (params: RegisterParams) => Promise<void>
  logout: () => Promise<void>
  refreshUser: () => Promise<void>
}

const AuthContext = createContext<AuthContextType | undefined>(undefined)

function getUserFromStorage(): User | null {
  if (typeof window === 'undefined') return null
  try {
    const userStr = localStorage.getItem(USER_KEY)
    return userStr ? JSON.parse(userStr) : null
  } catch {
    return null
  }
}

function setUserToStorage(user: User | null): void {
  if (typeof window === 'undefined') return
  if (user) {
    localStorage.setItem(USER_KEY, JSON.stringify(user))
  } else {
    localStorage.removeItem(USER_KEY)
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  // 始终以 loading + null 用户开始，避免 SSR/CSR 水合不匹配
  const [user, setUser] = useState<User | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const router = useRouter()
  const initRef = useRef(false)

  // 强制登出：清除本地状态并跳转登录页
  const forceLogout = useCallback(() => {
    removeToken()
    setUser(null)
    setUserToStorage(null)
    router.push('/login')
  }, [router])

  // 注册认证过期回调，供 fetch.ts 401 拦截器调用
  useEffect(() => {
    onAuthExpired(forceLogout)
  }, [forceLogout])

  const refreshUser = useCallback(async () => {
    try {
      const currentUser = await authApi.getCurrentUser()
      setUser(currentUser)
      setUserToStorage(currentUser)
    } catch {
      const cachedUser = getUserFromStorage()
      if (!cachedUser) {
        removeToken()
        setUser(null)
        setUserToStorage(null)
      }
    } finally {
      setIsLoading(false)
    }
  }, [])

  // 初始化：客户端 mount 后从 localStorage 恢复状态，有 token 时后台校验
  useEffect(() => {
    if (initRef.current) return
    initRef.current = true

    const token = getToken()
    const cachedUser = getUserFromStorage()

    if (token && cachedUser) {
      // 先用缓存用户消除闪烁，再后台校验
      // eslint-disable-next-line react-hooks/set-state-in-effect -- 初始化恢复缓存是合法模式
      setUser(cachedUser)
      refreshUser()
    } else if (token) {
      // 有 token 无缓存，后台获取
      refreshUser()
    } else {
      // 无 token，直接完成 loading
      setIsLoading(false)
    }
  }, [refreshUser])

  const login = async (params: LoginParams) => {
    const response = await authApi.login(params)
    setToken(response.access_token)
    setRefreshToken(response.refresh_token)
    try {
      const fullUser = await authApi.getCurrentUser()
      setUser(fullUser)
      setUserToStorage(fullUser)
    } catch {
      setUser(response.user)
      setUserToStorage(response.user)
    }
  }

  const register = async (params: RegisterParams) => {
    await authApi.register(params)
  }

  const logout = useCallback(async () => {
    // 通知后端将 token 加入黑名单
    try {
      const accessToken = getToken()
      const refreshToken = getRefreshToken()
      if (accessToken) {
        await fetch(`${API_CONFIG.baseURL}/auth/logout`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${accessToken}`,
            'X-Refresh-Token': refreshToken || '',
          },
        })
      }
    } catch {
      // 后端登出失败不阻塞前端登出流程
    }
    removeToken()
    setUser(null)
    setUserToStorage(null)
    router.push('/login')
  }, [router])

  return (
    <AuthContext.Provider value={{ user, isLoading, isAuthenticated: !!user, login, register, logout, refreshUser }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}
