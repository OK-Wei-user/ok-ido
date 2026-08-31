'use client'

import React from 'react'
import { usePathname, useRouter } from 'next/navigation'
import { SidebarProvider } from '@/components/ui/sidebar'
import { SessionsProvider } from '@/providers/sessions-provider'
import { AuthProvider, useAuth } from '@/providers/auth-provider'
import { Toaster } from '@/components/ui/sonner'
import { LeftPanel } from '@/components/left-panel'
import { Skeleton } from '@/components/ui/skeleton'

function AuthGuard({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth()
  const pathname = usePathname()
  const router = useRouter()

  const isPublicPath = pathname === '/login' || pathname === '/register'

  React.useEffect(() => {
    if (isLoading) return
    if (!isAuthenticated && !isPublicPath) {
      router.push('/login')
    } else if (isAuthenticated && isPublicPath) {
      router.push('/')
    }
  }, [isAuthenticated, isLoading, isPublicPath, router])

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="space-y-4">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-4 w-48" />
        </div>
      </div>
    )
  }

  if (!isAuthenticated && !isPublicPath) {
    return null
  }

  if (isAuthenticated && isPublicPath) {
    return null
  }

  return <>{children}</>
}

function LayoutContent({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const isAuthPage = pathname === '/login' || pathname === '/register'

  if (isAuthPage) {
    return <>{children}</>
  }

  return (
    <SessionsProvider>
      <SidebarProvider
        style={{
          '--sidebar-width': '300px',
          '--sidebar-width-icon': '300px',
        } as React.CSSProperties}
      >
        <LeftPanel />
        <div className="flex-1 bg-[#f8f8f7] h-screen overflow-hidden">
          {children}
        </div>
      </SidebarProvider>
    </SessionsProvider>
  )
}

export function RootLayoutClient({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <>
      <AuthProvider>
        <AuthGuard>
          <LayoutContent>{children}</LayoutContent>
        </AuthGuard>
      </AuthProvider>
      <Toaster position="top-center" richColors />
    </>
  )
}
