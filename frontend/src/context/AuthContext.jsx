import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import {
  createUserWithEmailAndPassword,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signOut,
} from 'firebase/auth'
import toast from 'react-hot-toast'

import { auth } from '../services/firebase'
import { activateUser, createAdminUser, getInviteStatus, getUserByEmail, setAuthToken } from '../services/api'

const AuthContext = createContext(null)

const defaultProfile = {
  role: 'manager',
  assignedIntents: [],
  status: 'unknown',
  businessId: '',
  inviteValidated: false,
}

const getAuthErrorMessage = (error) => {
  const statusCode = error?.response?.status
  const detail = error?.response?.data?.detail

  if (!error?.response && error?.request) {
    return 'Unable to reach the SalesAI API. Check that the backend is running and try again.'
  }
  if (statusCode === 401) {
    return detail || 'Your Firebase session is invalid. Sign in again.'
  }
  if (statusCode === 403) {
    return detail || 'Your account is not authorized to access SalesAI.'
  }
  if (statusCode >= 500) {
    return 'The SalesAI API failed while loading your account. Try again shortly.'
  }

  return detail || error?.message || 'Unable to load your SalesAI account.'
}

export const getDefaultDashboardRoute = (role) => {
  return role === 'admin' ? '/dashboard' : '/emails'
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [profile, setProfile] = useState(defaultProfile)
  const [loading, setLoading] = useState(true)
  const [authReady, setAuthReady] = useState(false)
  const [authError, setAuthError] = useState('')

  useEffect(() => {
    const unsubscribe = onAuthStateChanged(auth, async (firebaseUser) => {
      try {
        if (!firebaseUser) {
          setUser(null)
          setProfile(defaultProfile)
          setAuthToken(null)
          setAuthError('')
          return
        }

        setAuthError('')
        const token = await firebaseUser.getIdToken(true)
        setAuthToken(token)

        let backendUser = null
        try {
          backendUser = await getUserByEmail(firebaseUser.email)
        } catch (error) {
          const statusCode = error?.response?.status
          if (statusCode !== 404) {
            throw error
          }
        }

        if (!backendUser) {
          // First successful login can bootstrap an admin profile if backend record is missing.
          backendUser = await createAdminUser({
            email: firebaseUser.email,
            name: firebaseUser.displayName || firebaseUser.email,
            role: 'admin',
            business_id: (firebaseUser.email || '').split('@')[0] || firebaseUser.email,
          })
        }

        setUser(firebaseUser)
        setProfile({
          role: backendUser.role || 'manager',
          assignedIntents: Array.isArray(backendUser.assigned_intents)
            ? backendUser.assigned_intents
            : [],
          status: backendUser.status || 'unknown',
          businessId: backendUser.business_id || '',
          inviteValidated: backendUser.status === 'active',
          name: backendUser.name || firebaseUser.displayName || firebaseUser.email,
        })
      } catch (error) {
        await signOut(auth)
        setAuthToken(null)
        setUser(null)
        setProfile(defaultProfile)
        const message = getAuthErrorMessage(error)
        setAuthError(message)
        toast.error(message)
      } finally {
        setLoading(false)
        setAuthReady(true)
      }
    })

    return () => unsubscribe()
  }, [])

  const login = useCallback(async (email, password) => {
    setAuthError('')
    const credential = await signInWithEmailAndPassword(auth, email, password)
    const token = await credential.user.getIdToken(true)
    setAuthToken(token)
    toast.success('Logged in successfully')
  }, [])

  const validateInvite = useCallback(async (email) => {
    const normalized = (email || '').trim().toLowerCase()
    if (!normalized) {
      return { ok: false, reason: 'Email is required' }
    }

    try {
      const status = await getInviteStatus(normalized)
      if (!status?.invited) {
        return { ok: false, reason: 'You are not invited' }
      }
      return { ok: true, user: status }
    } catch (error) {
      return { ok: false, reason: 'You are not invited' }
    }
  }, [])

  const signup = useCallback(async ({ name, email, password, role = 'manager', businessId = '', requireInvite = true }) => {
    const normalizedEmail = (email || '').trim().toLowerCase()

    if (requireInvite) {
      const inviteCheck = await validateInvite(normalizedEmail)
      if (!inviteCheck.ok) {
        throw new Error(inviteCheck.reason || 'You are not invited')
      }
    }

    const credential = await createUserWithEmailAndPassword(auth, email, password)
    const token = await credential.user.getIdToken(true)
    setAuthToken(token)

    if (role === 'admin' && !requireInvite) {
      await createAdminUser({
        email: credential.user.email,
        name: (name || '').trim() || credential.user.email,
        role: 'admin',
        business_id: (businessId || '').trim() || credential.user.email,
      })
    } else {
      await activateUser({
        email: credential.user.email,
        firebase_uid: credential.user.uid,
      })
    }

    toast.success('Account created successfully')
  }, [validateInvite])

  const logout = useCallback(async () => {
    await signOut(auth)
    setAuthToken(null)
    toast.success('Logged out')
  }, [])

  const value = useMemo(
    () => ({
      user,
      profile,
      loading,
      authReady,
      authError,
      isAuthenticated: Boolean(user),
      isInviteValidated: profile.inviteValidated,
      isAdmin: profile.role === 'admin',
      defaultDashboardRoute: getDefaultDashboardRoute(profile.role),
      login,
      validateInvite,
      signup,
      logout,
    }),
    [user, profile, loading, authReady, authError, login, validateInvite, signup, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export const useAuth = () => {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth must be used inside AuthProvider')
  }
  return context
}
